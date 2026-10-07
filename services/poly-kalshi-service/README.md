# poly-kalshi-service

The kpc convergence trade, rebuilt to measure its one untested repair. On 15-minute
crypto up/down binaries, when Kalshi's YES mid and Polymarket's differ by at least
0.10, buy the cheap Polymarket leg as a taker, at or below 0.60, and hold to
settlement. Kalshi is the signal only.

The threshold is 0.10, not the manual's 0.20 (decided 2026-10-07): at 0.20 there
were almost no fills. Because the first crossing decides, the threshold defines
the trade, so this is a different strategy from the manual's, with its own gates
(below). Paper runs in Dublin, the closest location allowed to trade on
Polymarket; London is close-only and records only.

**Phases 1–2 only: record, replay, paper.** Nothing here can place a real order.
`PK_EXECUTION_MODE=live` is refused at startup until the paper gates pass and a host
in a permitted jurisdiction exists. The UK, US, DE and FR are all close-only on
Polymarket.

## Run

```bash
uv sync --package poly-kalshi-service --group dev --extra research
cp .env.example.dev .env.dev   # fill in API_KEY and the Kalshi key; chmod 600
poly-kalshi-service --profile dev      # 127.0.0.1:8050
pk-status --profile dev                # 127.0.0.1:8051, read-only page
```

| Route | |
|---|---|
| `GET /health/ready` | both feeds up and every asset's current window identified |
| `GET /v1/status` | feeds, recorder, per-asset window and quotes, ledger, risk (`X-API-Key`) |
| `GET /v1/recent` | last decisions, fills and settlements |
| `POST /v1/kill`, `POST /v1/kill/ack` | halt or resume paper entries (recording continues) |

Telegram `/kill` and `/status` run on a bot of its own (`PK_TELEGRAM_BOT_TOKEN`).

**Monitoring.** `pk-status` reads top to bottom:
1. **Alerts strip:** feeds down or stale, halts, crossed or missing books, unidentified windows, balance rejects, a paused recorder, stale data. Otherwise "all clear".
2. **Performance:**
   - *Capital & risk:* initial capital, cash, equity, realised and equity return %, capital in use, largest open exposure, max and current drawdown.
   - *Trade quality:* average win and loss, profit factor, expectancy, largest win and loss, longest losing streak, gross P&L and fees.
   - *Gates.*
3. **Live market:** the market state table (per asset bid/ask/mid, Δ against the threshold, what the rule would do now, book ages, this window's decision) and per-second Kalshi vs Polymarket charts.
4. **Edge:** the signal check (win rate vs Kalshi-implied vs price paid), the opportunity funnel (windows → crossings → skips → blocked → orders → fills → settled, total and today), and breakdowns by asset, by second and by entry band.
5. **History:** equity curve, daily results, the last 50 trades, recent engine events.
6. **System:** feeds, latency, execution, and data quality (invalid legs by cause, seq gaps, reconnects, unidentified windows, engine errors, positions awaiting settlement).

Results come from the journal on disk; live state comes from one cached read of the trader every 5 s, so viewers add no load. It runs at lower CPU priority behind a Cloudflare Tunnel with Access (see `infra/cloudflare/README.md`).

**Export all data (.zip)** downloads one file for offline analysis: journal, events log, ledger, metrics summary, recording manifests and the chart data. It contains no secrets. See `export.py` for the layout.
- Results come from the journal on disk.
- Live state comes from one cached read of `/v1/status` every 5 s, so viewers add no load on the trader.
- It runs at lower CPU priority and is published through a Cloudflare Tunnel with Access in front. See the poly-kalshi section of `infra/cloudflare/README.md`.

## How it works

```
Kalshi WS (orderbook_delta) ─┐
Polymarket WS (market) ──────┼─▶ Runtime.ingest: one clock ─▶ Recorder (tape)
control lines (windows,      │                             └▶ Engine.handle_line
  resolutions, reconnects) ──┘                                  ├ books per window id
                                                                ├ decide() on every update
                                                                ├ PaperVenue (+latency, walk asks)
                                                                └ Ledger (cash; payout credited)
```

- **Window identity** (`windows.py`):
  - Polymarket's slug is `<asset>-updown-15m-<open_unix>`, with `clobTokenIds = [Up, Down]`.
  - Kalshi's series is `KX<ASSET>15M`; the market is the one whose `close_time = open + 900`.
  - The next window is discovered `PK_DISCOVER_LEAD_SECONDS` ahead and written to the tape.
- **Ghost gap** (kpc trap 3):
  - Books are keyed by the window's own ids, so an old window's book can't be read as the new one's.
  - Old books are dropped at the roll.
  - A leg counts only if it has updated since the window opened.
- **Decision** (`decision.py`): manual §3 exactly, with three rules:
  - The first crossing decides.
  - A crossing above the cap consumes the window.
  - No exit, ever.
- **Paper fill** (`venue.py`):
  - The order meets the book `PK_PAPER_LATENCY_MS` after the decision.
  - It walks the asks up to the limit; partial fills are kept and the rest is cancelled.
  - Fees are computed per level from `feeSchedule`.
  - The naive touch fill is logged next to it for comparison.
- **Settlement** (`ledger.py`):
  - The source is Polymarket's resolution: the `market_resolved` WS event, or a Gamma poll.
  - The payout is credited to cash (kpc trap 4).
  - Kalshi's result is recorded too, to measure venue disagreement.
- **Journal**: `data/state/<profile>/journal/YYYY-MM-DD.jsonl` holds every decision, fill, settlement, resolution and window summary.

## Results epochs

Every journal record carries `epoch` (`PK_RESULTS_EPOCH`), `build` (the git commit) and `config` (a hash of the strategy parameters).
- The status page, `research.readout` and the export use only the current epoch, and the page header shows how many older records were set aside.
- Changing the epoch archives the paper ledger (`ledger.epoch-<old>.json`) and starts a fresh one.
- Bump the epoch whenever a fix invalidates earlier results.
- Records from before epochs existed, i.e. the ghost-level period, are always excluded.

## Research

```bash
python -m research.replay run --record-dir data/raw/dev --out research/data/run1
python -m research.replay compare --live data/state/dev/journal --replay research/data/run1/journal
python -m research.nulls --run research/data/run1
python -m research.readout --journal data/state/dev/journal
```

- `replay run` re-runs the tape through the live engine. It writes a journal and a per-second `grid.parquet`.
- `replay compare` must show 100% agreement with live on the same tape.
- `nulls` is the manual's null battery. Expect `signal` ≈ +5 pp and `random_same_rule` ≈ 0 pp.
- `readout` reports the gates below, the shadow-variant table, markouts and capacity.

### The Helix tape (`~/kpc/raw`, 73 days of best bid/ask)

```bash
export HELIX_TAPE_DIR=~/kpc/raw
python -m research.helix.validate --day 20260920          # decoder checks (PLAN 0.1a)
python -m research.helix.resolutions                      # venue outcomes, regime, fee
python -m research.helix_replay run --jobs 3              # decide() over every window
python -m research.helix_sweeps --split discovery         # sweeps, nulls, latency (3.3)
```

`research/helix/README.md` documents the format, the clocks and what the
validation found. Only derived outputs are written (`research/data/helix/<day>/`);
the tape is never copied.

## Gates (re-registered 2026-10-07 for the 0.10 strategy, before any new result was read)

The manual's magnitudes ($/entry +4.4 to +6.9, a +5 pp signal, the band shape)
were measured at 0.20 and do not apply to 0.10. They are still reported, never
gated. `gates.py` computes these; the status page and `research.readout` show them.

- **Primary: day-t ≥ 2 at n ≥ 300 *filled* entries at or below 0.60, net of fees,
  at the real fill (VWAP), not the touch. Kill if not met.**
- **Adverse selection:** the median 5 s markout on fills is no worse than −3¢.
  If it is worse, the edge belongs to faster traders and we are their exit.
- **Capacity:** the median $ filled per entry is reported per asset. An asset
  under $10 at the touch can't carry live size, whatever its edge.
- **Null battery:** `signal` beats `random_same_rule` by the margin fixed in
  `research/PREREG.md`, derived from the Helix discovery days of the TWAP
  regime and written down before the holdout ran.
- Average and p90 fill ≤ 0.60; balance rejects alarm before P&L goes flat.

## What changed since the kpc manual (checked 2026-10-06)

- **Polymarket now settles on a Chainlink 60 s TWAP** (`btc-usd-twap-60s-streams`), and Kalshi on a 60 s average of the CF Benchmarks RTI.
  - The switch was on **2026-08-07** (Gamma's `resolutionSource` per window: the spot stream through 08-06, the TWAP stream from 08-07).
  - Venue disagreement over the Helix days: 6.8% before the switch (the manual's 5.6–6.8%), **1.6% after it**.
  - The edge itself may have moved with it, so re-measure before trusting the manual's magnitudes.
- Polymarket's YES and NO books mirror each other: one `price_change` updates both tokens. Both books are still kept.
- Polymarket sent about 270 `price_change` frames a second for one BTC window, far above the manual's 159/s across everything. Every `price_change` carries the venue's best bid/ask, and the engine counts disagreements with its own book (`bbo_mismatch_pc`). On the first tape (1.07M checks), ours matched 99.86%; the misses were the venue's field lagging its own deltas. The separate `best_bid_ask` event is one update stale (97% disagreement), so never price off it.
- Kalshi's WS adds and drops tickers on the open socket (`update_subscription`), so there is no teardown at the roll.
- Polymarket moved to CLOB V2 on 2026-04-28 (pUSD collateral, a new order struct). Phase 3 starts from `py-clob-client-v2`, not the Helix adapter.
- Gamma returns 403 to Python's default user agent, so discovery sends its own.
