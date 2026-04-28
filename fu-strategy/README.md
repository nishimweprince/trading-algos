# FU Strategy

FastAPI service for the FU candle / multi-timeframe strategy: Capital.com data feed, HTF bias and zones, LTF FU triggers, MTF confluence, and paper/live execution hooks.

## How it works (end-to-end)

```
┌─────────────────────────────────────────────────────────────┐
│  Capital.com REST + WebSocket  (FX candles, account, orders)│
└────────────────┬────────────────────────────────────────────┘
                 │
        ┌────────▼────────┐
        │  CapitalDataFeed │  fetch + DTO normalize
        └────────┬────────┘
                 │
       ┌─────────▼─────────┐
       │  Rolling buffers  │  per (symbol, timeframe), deque
       └─────────┬─────────┘
                 │ on each new candle
     ┌───────────┴────────────┐
     │                        │
┌────▼─────┐         ┌────────▼─────────┐
│   HTF     │         │       LTF        │
│ 4H / 1H   │         │   15m / 5m       │
└────┬─────┘         └────────┬─────────┘
     │                        │
     │ structure.py           │ fu_candle.py
     │ → Bias                 │ → BullFU / BearFU
     │                        │
     │ zones.py               │
     │ → active demand/       │
     │   supply zones         │
     │                        │
     └────────┐    ┌──────────┘
              │    │
        ┌─────▼────▼──────────┐
        │   mtf_aggregator    │  align HTF bias + zones with LTF entry
        └──────────┬──────────┘
                   │
            ┌──────▼───────┐
            │ Confluence?  │
            │  FU + Zone   │
            │  + Bias      │
            └──────┬───────┘
                   │ yes
            ┌──────▼───────┐
            │  Signal DTO  │  entry, SL, TP, zone_id, bias
            └──────┬───────┘
                   │
        ┌──────────┼─────────────┐
        ▼          ▼             ▼
   JSONL log   /signals API   live_engine
                              (paper_mode=true)
                              → log "intended order",
                                no real fill
```

### Lifecycle (per candle)

1. WebSocket emits a closed LTF candle (e.g. 5m for EURUSD).
2. Buffer appends; resampler updates the matching HTF bucket if a 1H/4H boundary just closed.
3. `structure.py` updates HTF bias (BULLISH / BEARISH / NEUTRAL).
4. `zones.py` ages, mitigates, and converts HTF zones; emits the current ACTIVE list.
5. `fu_candle.py` checks the just-closed LTF candle for bull/bear FU.
6. If FU fires AND bias aligns AND price is inside a confluent HTF zone → emit Signal.
7. `live_engine` in `paper_mode` logs the intended entry/SL/TP without hitting Capital.com order endpoints.
8. `/signals` and `/zones` REST endpoints expose state for monitoring.

---

## Timeframes

The strategy is multi-timeframe by design — bias is decided slowly, entries fire fast.

| Role | Timeframes | What it does | Why this TF |
| --- | --- | --- | --- |
| **HTF Bias** | 4H, 1H | Runs `structure.py`. CHoCH/BOS up → BULLISH; CHoCH/BOS down → BEARISH. Only allow longs in BULLISH state, shorts in BEARISH. | Slow enough to filter noise; 4H sets the dominant trend, 1H sets the working trend. |
| **HTF Zones** | 4H, 1H | `zones.py` builds supply/demand zones from confirmed swing sweeps on these TFs. | Zones drawn here have meaningful institutional reaction; LTF zones are too noisy for this strategy. |
| **LTF Entry** | 15m, 5m | `fu_candle.py` looks for a sweep + close-beyond on every closed candle. | Tight risk: SL fits inside the zone, R:R is favorable. |
| **(Defer) Confirmation** | 1m | Optional refinement / micro-trigger. Not in MVP. | Phase 2. |

**Rule of thumb the engine enforces:**

- Bias decided on **HTF** (4H, 1H).
- Zones drawn on **HTF** (4H, 1H).
- FU entry trigger on **LTF** (15m, 5m), but **only** when LTF price is inside a HTF zone AND HTF bias aligns.

If you want longer-horizon swings, add 1D to HTF; for scalping, drop LTF to 1m. Both are config flips, not code changes.

---

## Environment variables

Put these in a `.env` file in this project directory (`fu-strategy/`). Copy `.env.example` to `.env` and edit. Capital.com provides the first three from your account dashboard. Wire `pydantic-settings` (or equivalent) to load `.env` in application code.

### Required

| Var | Example | Notes |
| --- | --- | --- |
| `CAPITAL_API_KEY` | `abc123...` | From Capital.com → Settings → API integrations. Custom API keys are tied to one environment (demo or live). |
| `CAPITAL_IDENTIFIER` | `you@example.com` | Account email/username. |
| `CAPITAL_PASSWORD` | `your-account-password` | Plain text in `.env`; the client RSA-encrypts it before sending. |
| `CAPITAL_ENVIRONMENT` | `demo` | `demo` or `live`. Stay on `demo` for paper-trading MVP. |

### Strategy config (defaults; override only if needed)

| Var | Default | Notes |
| --- | --- | --- |
| `SYMBOLS` | `EUR_USD,GBP_USD,USD_JPY` | Comma-separated. Use the standard `XXX_YYY` format; the mapper translates to Capital.com epics. |
| `HTF_TIMEFRAMES` | `4H,1H` | Bias + zones run here. |
| `LTF_TIMEFRAMES` | `15M,5M` | FU entry trigger runs here. |
| `PAPER_MODE` | `true` | Disables real order placement. Set `false` only after you've reviewed paper signals. |
| `RISK_PER_TRADE_PCT` | `0.5` | Percent of equity risked per trade (used by the position sizer). |
| `BACKFILL_CANDLES` | `500` | Per-TF history fetched on startup to seed indicators. |

### FU indicator (defaults match the Pine script)

| Var | Default | Notes |
| --- | --- | --- |
| `FU_USE_DOJI_FILTER` | `false` | Require prior candle to be a doji. |
| `FU_USE_MA_FILTER` | `false` | Require close vs SMA agreement. |
| `FU_SMA_LENGTH` | `9` | |
| `FU_DOJI_BODY_RATIO` | `0.3` | Body ≤ 30% of range = doji. |

### Operational

| Var | Default | Notes |
| --- | --- | --- |
| `DATABASE_URL` | `sqlite+aiosqlite:///./fu_strategy.db` | Persistence layer (when wired). |
| `LOG_LEVEL` | `INFO` | `DEBUG` for verbose. |
| `SIGNAL_LOG_PATH` | `./logs/signals.jsonl` | One signal per line, paper-trading audit trail. |

### Example `.env`

```env
CAPITAL_API_KEY=replace_me
CAPITAL_IDENTIFIER=you@example.com
CAPITAL_PASSWORD=replace_me
CAPITAL_ENVIRONMENT=demo

SYMBOLS=EUR_USD,GBP_USD,USD_JPY
HTF_TIMEFRAMES=4H,1H
LTF_TIMEFRAMES=15M,5M
PAPER_MODE=true
RISK_PER_TRADE_PCT=0.5
BACKFILL_CANDLES=500

LOG_LEVEL=INFO
SIGNAL_LOG_PATH=./logs/signals.jsonl
```

---

## Python on your PATH (macOS)

If `python` is not found but Homebrew is installed, `python3` is usually at `/opt/homebrew/bin/python3`.

1. **Put Homebrew on PATH** (add to `~/.zshrc` if missing):

   ```bash
   eval "$(/opt/homebrew/bin/brew shellenv)"
   ```

   Or explicitly:

   ```bash
   export PATH="/opt/homebrew/bin:/opt/homebrew/sbin:$PATH"
   ```

2. **Use a virtual environment** (recommended): after activation, `python` and `pip` point at the venv.

   ```bash
   cd fu-strategy
   python3 -m venv .venv
   source .venv/bin/activate
   python -m pip install -U pip
   pip install -e .
   ```

3. **Optional: `python` → `python3` in zsh** (only if you want a global alias; venv is safer):

   ```bash
   alias python=python3
   ```

Reload the shell: `source ~/.zshrc`.

---

## Run the API

```bash
cd fu-strategy
source .venv/bin/activate
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

Or use the helper script (uses `.venv/bin/uvicorn` when present):

```bash
cd fu-strategy
./scripts/run.sh
```

Override host/port: `HOST=127.0.0.1 PORT=9000 ./scripts/run.sh`

With Make: `make run` (after `make install`).

- OpenAPI: http://127.0.0.1:8000/docs  
- Health: http://127.0.0.1:8000/health

---

## Dev dependencies

```bash
pip install -e ".[dev]"
```
