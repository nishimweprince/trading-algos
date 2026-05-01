# FU Strategy

FastAPI service for the FU candle / multi-timeframe strategy: Capital.com data feed, HTF bias and zones, realtime forming-candle LTF FU triggers, MTF confluence, notifications on slow timeframes, and live auto-execution on 1M.

## How it works (end-to-end)

```
┌─────────────────────────────────────────────────────────────┐
│        Capital.com REST  (FX candles, account, orders)       │
└────────────────┬────────────────────────────────────────────┘
                 │
        ┌────────▼────────┐
        │  CapitalDataFeed │  absolute-minute poll + DTO normalize
        └────────┬────────┘
                 │
       ┌─────────▼─────────┐
       │ Indicator state   │  per (symbol, timeframe), DataFrame
       └─────────┬─────────┘
                 │ closed candles update state; latest bar previews FU
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
            ┌──────▼───────┐
            │ Timeframe?   │
            └──┬──────┬────┘
               │      │
       1M signal      Other timeframes (15M, 5M, …)
               │      │
        ┌──────▼──┐   └─────────┬──────────────┐
        │  Trade   │             ▼              ▼
        │ Executor │       JSONL log    NotificationDispatcher
        │ (live)   │      /signals API  → WhatsApp Cloud API
        └──────────┘                    → Pindo SMS API
        SL/TP anchored                  → JSONL log
        to live bid/offer               → /notifications API
        → POST /positions
```

### Lifecycle (per candle)

1. The poller wakes on absolute minute boundaries (`15:02:34` start → first poll at `15:03:00`, then `15:04:00`, etc.).
2. Capital.com REST returns a small candle tail; closed candles advance indicator state and the latest candle is treated as the forming candle.
3. `structure.py` updates HTF bias (BULLISH / BEARISH / NEUTRAL).
4. `zones.py` ages, mitigates, and converts HTF zones; emits the current ACTIVE list.
5. `fu_candle.py` checks the current forming LTF candle for bull/bear FU signals.
6. If the forming FU fires AND bias aligns AND price is inside a confluent HTF zone → emit Signal. Closed-candle FU events are logged/stateful only and do not create trade signals.
7. **1M signals** are routed to `TradeExecutor` (`app/execution/trade_executor.py`), which fetches live bid/offer from `/api/v1/markets/{epic}`, anchors SL/TP to current market price (so stale signal levels never end up on the wrong side of price), pushes them clear of the broker's `minControlledRiskStopDistance` rule, and places a real position via `POST /api/v1/positions`. No notification is sent.
8. **All other timeframes** flow through `NotificationDispatcher`, which fans the signal out to every recipient in `NOTIFICATION_NUMBERS` through the enabled channels (`whatsapp`, `sms`), appending each lifecycle event to `NOTIFICATIONS_LOG_PATH`.
9. `/signals`, `/zones`, and `/notifications` REST endpoints expose state for monitoring. Each placed order appends an `execution` event to `INDICATOR_EVENT_LOG_PATH` with `signal_sl`, `signal_tp`, `market_bid`, `market_offer`, the rounded `stop_level`/`profit_level`, and `dealReference`.

---

## Notifications (WhatsApp + SMS)

Every generated `Signal` is sent to each phone number in `NOTIFICATION_NUMBERS` through the channels enabled by `NOTIFICATION_CHANNELS`. WhatsApp uses the [WhatsApp Cloud API](https://developers.facebook.com/docs/whatsapp/cloud-api); SMS uses the Pindo API. Sends, failures, and WhatsApp delivery receipts are appended to a JSONL log.

### Components

| Module | Role |
| --- | --- |
| `app/notifications/whatsapp_client.py` | Async client: `send_text`, `send_template`, GET-verify handshake, `X-Hub-Signature-256` HMAC verification. |
| `app/notifications/sms_client.py` | Async Pindo SMS client used for concise signal texts. |
| `app/notifications/dispatcher.py` | `notify_signal(signal)` fans out to all recipients and enabled channels; `send_test(...)` for ad-hoc messages. Errors are logged per-recipient and don't block the others. |
| `app/notifications/log.py` | JSONL append-only log. One line per state transition. Latest state per `id` is materialized at read time. |
| `app/notifications/formatters.py` | Renders a `Signal` to rich free-form text, compact SMS text, or the existing WhatsApp template-parameter list. |
| `app/api/webhooks.py` | `GET /webhooks/whatsapp` — Meta verification handshake. `POST /webhooks/whatsapp` — signed delivery events; updates the log status by `wamid`. |
| `app/api/notifications.py` | `GET /notifications` (history, filterable), `GET /notifications/{id}`, `POST /notifications/test`, `POST /notifications/test/broadcast`. |

### Lifecycle and log shape

```
pending  →  sent       →  delivered  →  read       (happy path)
         →  failed                                  (Graph API rejected)
```

Each transition appends one JSON object to `NOTIFICATIONS_LOG_PATH`. Records share an `id` (UUID); `list_recent` collapses them to the latest snapshot per id. The `channel` field is `whatsapp` or `sms`. Example line:

```json
{"id":"…","signal_id":"…","recipient":"+447700900000","message_type":"text","body":"…","status":"sent","wamid":"wamid.HBg…","error":null,"created_at":"…","sent_at":"…","updated_at":"…","channel":"whatsapp"}
```

Signal free-form messages include direction, symbol/timeframe, entry, SL, TP, R:R, bias, swept previous high/low, FU candle OHLC, confluence, and FU time. SMS messages are capped to one compact segment (`SMS_MAX_CHARS = 150`) and prioritize direction, symbol/timeframe, entry, SL, TP, R:R, bias, swept level, and FU time.

### Free-form vs template

- **Free-form text** (`WHATSAPP_TEMPLATE_NAME` empty): only delivers when the recipient has messaged the business in the last 24 hours. Useful for development or recipients in an open session.
- **Template** (`WHATSAPP_TEMPLATE_NAME` set): required for unsolicited notifications. The template body must contain placeholders `{{1}}…{{8}}` mapped (in order) to: direction, symbol, timeframe, entry, SL, TP, R:R, bias.
- **SMS** (`sms` in `NOTIFICATION_CHANNELS` and `PINDO_TOKEN` set): sends compact Pindo SMS text. SMS does not use WhatsApp templates.

### Meta dashboard configuration

1. App Dashboard → **Use cases → Customize → Connect on WhatsApp → Basic setup → Step 2: Production setup → Configure Webhooks**.
2. Callback URL: `https://<your-host>/webhooks/whatsapp` (HTTPS required — use ngrok or similar to expose `localhost`).
3. Verify token: paste the same string you set in `WHATSAPP_VERIFY_TOKEN`.
4. Subscribe to the `messages` field so delivery/read receipts update the log.
5. (Optional) Per-WABA routing: `POST /{WABA_ID}/subscribed_apps` with `override_callback_uri` — managed in the Meta dashboard, not in this app.

### Verifying setup

```bash
# Manual ad-hoc send (bypasses signal formatting)
curl -X POST http://127.0.0.1:8000/notifications/test \
  -H 'Content-Type: application/json' \
  -d '{"recipient":"+447700900000","message":"hello from fu-strategy"}'

# Broadcast to every NOTIFICATION_NUMBERS recipient through enabled channels
curl -X POST http://127.0.0.1:8000/notifications/test/broadcast \
  -H 'Content-Type: application/json' \
  -d '{"message":"hello from fu-strategy"}'

# Inspect the log
curl http://127.0.0.1:8000/notifications
tail -f logs/notifications.jsonl
```

`POST /notifications/test` returns `{"log_ids":[...]}` because one request can create both WhatsApp and SMS log rows. `POST /notifications/test/broadcast` returns all log ids plus `recipients_attempted`.

---

## Timeframes

The strategy is multi-timeframe by design — bias is decided slowly, entries fire fast.

| Role | Timeframes | What it does | Why this TF |
| --- | --- | --- | --- |
| **HTF Bias** | 4H, 1H | Runs `structure.py`. CHoCH/BOS up → BULLISH; CHoCH/BOS down → BEARISH. Only allow longs in BULLISH state, shorts in BEARISH. | Slow enough to filter noise; 4H sets the dominant trend, 1H sets the working trend. |
| **HTF Zones** | 4H, 1H | `zones.py` builds supply/demand zones from confirmed swing sweeps on these TFs. | Zones drawn here have meaningful institutional reaction; LTF zones are too noisy for this strategy. |
| **LTF Entry** | 15m, 5m | `fu_candle.py` looks for a sweep + close-beyond on the current forming candle. Signals here are sent to WhatsApp/SMS recipients. | Tight risk: signals arrive intrabar for scalping and shorter stops. |
| **LTF Auto-execute** | 1m | FU on 1m forming candle is routed to `TradeExecutor` and placed directly on Capital.com (no notification). | Fastest reaction window for scalping; manual delivery is too slow. |

**Rule of thumb the engine enforces:**

- Bias decided on **HTF** (4H, 1H).
- Zones drawn on **HTF** (4H, 1H).
- FU entry trigger on **LTF** (15m, 5m), but **only** when LTF price is inside a HTF zone AND HTF bias aligns.

If you want longer-horizon swings, add 1D to HTF; for scalping, drop LTF to 1m. Both are config flips, not code changes.

---

## Auto-execution on 1M

When `CAPITAL_EXECUTION_ENABLED=true` and `1M` is in `LTF_TIMEFRAMES`, every FU signal that fires on the 1-minute timeframe is sent directly to Capital.com instead of being broadcast as a notification.

### Why anchor SL/TP to live market

The signal's `entry_price`, `sl`, and `tp` are computed from the FU candle's close. By the time the order reaches the broker — even at 1-minute polling cadence — price has usually moved enough that those levels can land on the wrong side of current market (a SELL stop below current bid, or a BUY stop above current offer), which the broker rejects with `error.invalid.stoploss.minvalue` or `error.invalid.stoploss.maxvalue`. To avoid this entirely, `TradeExecutor` reads live bid/offer from `/api/v1/markets/{epic}` and computes SL/TP as a percentage of the current entry price:

| Direction | Entry | SL | TP |
| --- | --- | --- | --- |
| BUY | `offer` | `offer × (1 − SL_PCT/100)` | `offer × (1 + SL_PCT/100 × RR_TARGET)` |
| SELL | `bid` | `bid × (1 + SL_PCT/100)` | `bid × (1 − SL_PCT/100 × RR_TARGET)` |

With defaults `CAPITAL_EXECUTION_SL_PCT=0.5` and `RR_TARGET=2.0`:

| Instrument | Approx SL distance | Approx TP distance |
| --- | --- | --- |
| EUR/USD @ 1.10 | ~50 pips | ~100 pips |
| Gold @ 4600 | ~$23 | ~$46 |
| BTC/USD @ 77000 | ~$385 | ~$770 |
| S&P 500 @ 5000 | ~25 pts | ~50 pts |

After the percentage calculation, levels are pushed further out if they don't clear the broker's minimum-distance rule (`minControlledRiskStopDistance` for guaranteed stops, otherwise `minNormalStopOrLimitDistance`) plus the live spread, multiplied by `CAPITAL_EXECUTION_SAFETY_MULTIPLIER`. PERCENTAGE-unit broker rules (common on crypto) are converted to absolute prices automatically.

### Skipped vs failed orders

`TradeExecutor` writes one `execution` event per attempt to `INDICATOR_EVENT_LOG_PATH`:

| Status | When |
| --- | --- |
| `PLACED` | Order accepted by Capital.com. Includes `dealReference`, `signal_sl`/`signal_tp` (original strategy levels), `stop_level`/`profit_level` (what was actually sent), and `market_bid`/`market_offer`. |
| `FAILED` | Broker rejected the request (e.g. distance still too tight, market closed). Error message logged; the poller keeps running. |
| `SKIPPED` | No live market data available (snapshot missing bid/offer, or `get_market_details` errored). Order is not attempted at all rather than placed with stale prices. |

### Disabling, dry-running, and rollback

- Leave `CAPITAL_EXECUTION_ENABLED=false` (default) to keep the legacy behavior — every signal goes to WhatsApp/SMS regardless of timeframe.
- Drop `1M` from `LTF_TIMEFRAMES` to stop generating 1M signals altogether.
- Set `CAPITAL_EXECUTION_USE_MARKET_DISTANCE=false` to send the signal's own `sl`/`tp` to the broker instead of percentage-anchored levels (the broker-distance safety net still runs).
- Stay on `CAPITAL_ENVIRONMENT=demo` until you've reviewed several live trades.

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

### Capital.com auto-execution (1M)

| Var | Default | Notes |
| --- | --- | --- |
| `CAPITAL_EXECUTION_ENABLED` | `false` | Master kill switch. When `true`, signals on `CAPITAL_EXECUTION_TIMEFRAME` place a real position via `POST /api/v1/positions` instead of being notified. |
| `CAPITAL_EXECUTION_TIMEFRAME` | `1M` | Which timeframe auto-executes. Must also be present in `LTF_TIMEFRAMES`. |
| `CAPITAL_EXECUTION_SIZE` | `1.0` | Fixed deal size, in broker contract units. Overridden upward by the broker's `minDealSize` if smaller. |
| `CAPITAL_EXECUTION_GUARANTEED_STOP` | `false` | Pass-through to `create_position`. When `true`, the broker honours the SL even on gaps (and the stricter `minControlledRiskStopDistance` rule applies). |
| `CAPITAL_EXECUTION_USE_MARKET_DISTANCE` | `true` | Anchor SL/TP to live bid/offer using `CAPITAL_EXECUTION_SL_PCT`. Set to `false` to send the signal's own `sl`/`tp` (still passed through the broker-distance safety net). |
| `CAPITAL_EXECUTION_SL_PCT` | `0.5` | SL as a percentage of live entry price. TP distance = `SL_PCT × RR_TARGET`. Defaults give ~50 pips on FX, ~$23 on gold, ~$385 on BTC at $77k, ~25 pts on indices. |
| `CAPITAL_EXECUTION_SAFETY_MULTIPLIER` | `1.5` | Multiplier on the broker's minimum stop distance, plus current spread, used as the final clearance buffer before placing the order. |

### Strategy config (defaults; override only if needed)

| Var | Default | Notes |
| --- | --- | --- |
| `SYMBOLS` | `EUR_USD,GBP_USD,USD_JPY` | Comma-separated. Use the standard `XXX_YYY` format; the mapper translates to Capital.com epics. |
| `HTF_TIMEFRAMES` | `4H,1H` | Bias + zones run here. |
| `LTF_TIMEFRAMES` | `15M,5M,1M` | FU entry trigger runs here. Include `1M` for auto-execution; signals on 15M/5M (and any other LTF) flow to notifications. |
| `PAPER_MODE` | `true` | Disables real order placement. Set `false` only after you've reviewed paper signals. |
| `RISK_PER_TRADE_PCT` | `0.5` | Percent of equity risked per trade (used by the position sizer). |
| `BACKFILL_CANDLES` | `500` | Per-TF history fetched on startup to seed indicators. |
| `POLLING_ENABLED` | `false` | Starts the Capital.com poller when credentials and symbols are configured. |
| `POLLING_INTERVAL_SECONDS` | `60` | Kept for compatibility; live polling now runs on absolute minute boundaries. |
| `POLLING_TAIL_CANDLES` | `5` | Candle tail fetched on each poll. |

### FU indicator

| Var | Default | Notes |
| --- | --- | --- |
| `FU_USE_DOJI_FILTER` | `false` | Require prior candle to be a doji. |
| `FU_USE_MA_FILTER` | `false` | Require close vs SMA agreement. |
| `FU_SMA_LENGTH` | `50` | Overrides the app default when set in `.env`. |
| `FU_DOJI_BODY_RATIO` | `0.3` | Body ≤ 30% of range = doji. |
| `FU_ONLY_MODE` | `true` | Emit FU-only signals without requiring HTF zones/bias. Set `false` for full confluence gating. |
| `FU_FIRE_ON_FORMING` | `true` | Required for realtime forming-candle FU signals. Closed-candle FU events do not emit trade signals. |

### Operational

| Var | Default | Notes |
| --- | --- | --- |
| `DATABASE_URL` | `sqlite+aiosqlite:///./fu_strategy.db` | Persistence layer (when wired). |
| `LOG_LEVEL` | `INFO` | `DEBUG` for verbose. |
| `SIGNAL_LOG_PATH` | `./logs/signals.jsonl` | One signal per line, paper-trading audit trail. |
| `NOTIFICATIONS_LOG_PATH` | `./logs/notifications.jsonl` | Append-only notification send/receipt log; one event per line. |

### WhatsApp Cloud API

| Var | Default | Notes |
| --- | --- | --- |
| `WHATSAPP_ACCESS_TOKEN` | _none_ | **Required.** Permanent or temporary token from Meta App Dashboard → WhatsApp → API Setup. |
| `WHATSAPP_PHONE_NUMBER_ID` | _none_ | **Required.** The sender phone number's ID (not the number itself). |
| `WHATSAPP_BUSINESS_ACCOUNT_ID` | _none_ | Optional. WABA ID, used if you set per-WABA `override_callback_uri` via `POST /{WABA_ID}/subscribed_apps`. |
| `WHATSAPP_API_VERSION` | `v21.0` | Graph API version. |
| `WHATSAPP_VERIFY_TOKEN` | _none_ | Any random string. Must match the value entered in the Meta webhook config — used to validate the GET-verify handshake. |
| `WHATSAPP_APP_SECRET` | _none_ | Meta app secret. Used to verify `X-Hub-Signature-256` on inbound webhook events. |
| `WHATSAPP_TEMPLATE_NAME` | _none_ | If empty, dispatcher sends free-form text (24h window only). If set, signals are sent as templated messages. |
| `WHATSAPP_TEMPLATE_LANGUAGE` | `en_US` | Template language code. |

### Pindo SMS API

| Var | Default | Notes |
| --- | --- | --- |
| `PINDO_API_URL` | `https://api.pindo.io/v1/sms/` | Pindo SMS endpoint. |
| `PINDO_TOKEN` | _none_ | Bearer token. Required when `sms` is enabled in `NOTIFICATION_CHANNELS`. |
| `PINDO_SENDER_ID` | `FUStrategy` | Sender name/id sent to Pindo. |

### Notification routing

| Var | Default | Notes |
| --- | --- | --- |
| `NOTIFICATIONS_ENABLED` | `true` | Master switch. Disable to silence all sends without removing credentials. |
| `NOTIFICATION_CHANNELS` | `whatsapp,sms` | Comma-separated allow-list. Supported values: `whatsapp`, `sms`. |
| `NOTIFICATION_NUMBERS` | _empty_ | Comma-separated list of recipients in E.164 format (e.g. `+14155552671,+447700900000`). |

### Example `.env`

```env
# Capital.com
CAPITAL_API_KEY=replace_me
CAPITAL_IDENTIFIER=you@example.com
CAPITAL_PASSWORD=replace_me
CAPITAL_ENVIRONMENT=demo

# Capital.com auto-execution (1M)
CAPITAL_EXECUTION_ENABLED=false
CAPITAL_EXECUTION_TIMEFRAME=1M
CAPITAL_EXECUTION_SIZE=1.0
CAPITAL_EXECUTION_GUARANTEED_STOP=false
CAPITAL_EXECUTION_USE_MARKET_DISTANCE=true
CAPITAL_EXECUTION_SL_PCT=0.5
CAPITAL_EXECUTION_SAFETY_MULTIPLIER=1.5

# Strategy
SYMBOLS=EUR_USD,GBP_USD,USD_JPY
HTF_TIMEFRAMES=4H,1H
LTF_TIMEFRAMES=15M,5M,1M
PAPER_MODE=true
RISK_PER_TRADE_PCT=0.5
BACKFILL_CANDLES=500
POLLING_ENABLED=true

# FU indicator
FU_USE_DOJI_FILTER=false
FU_USE_MA_FILTER=false
FU_SMA_LENGTH=50
FU_DOJI_BODY_RATIO=0.3
FU_ONLY_MODE=true
FU_FIRE_ON_FORMING=true

# Operational
LOG_LEVEL=INFO
SIGNAL_LOG_PATH=./logs/signals.jsonl
NOTIFICATIONS_LOG_PATH=./logs/notifications.jsonl

# WhatsApp Cloud API (Meta)
WHATSAPP_ACCESS_TOKEN=
WHATSAPP_PHONE_NUMBER_ID=
WHATSAPP_VERIFY_TOKEN=
WHATSAPP_APP_SECRET=
WHATSAPP_TEMPLATE_NAME=
WHATSAPP_TEMPLATE_LANGUAGE=en_US

# Pindo SMS
PINDO_API_URL=https://api.pindo.io/v1/sms/
PINDO_TOKEN=
PINDO_SENDER_ID=FUStrategy

# Notification routing
NOTIFICATIONS_ENABLED=true
NOTIFICATION_CHANNELS=whatsapp,sms
NOTIFICATION_NUMBERS=
```

The complete list of supported variables (with comments) lives in `.env.example` — copy it to `.env` and fill in.

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
