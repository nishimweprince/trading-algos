# poly-kalshi-service

The kpc convergence trade, rebuilt to measure its one untested repair. On 15-minute
crypto up/down binaries, when Kalshi's YES mid and Polymarket's differ by at least
0.20, buy the cheap Polymarket leg as a taker, at or below 0.60, and sell it once
Polymarket's bid catches up with Kalshi's price (the active exit), else hold to
settlement. Kalshi is the signal only; Binance, Kraken and Bitstamp are recorded and
compared (lead–lag panel) but never decide: from Dublin Binance arrives after both
venues move, and Kraken (London) is being measured.

**Epoch 14 (2026-10-14).** Execution economics, and live code only:
- **Exits net a cent after both fees.** The floor was entry + entry fee + 1¢,
  so a sale there lost the exit's own fee (≈ −0.7¢/share near 0.50). It is now
  the lowest price whose proceeds after the exit fee cover the entry, its fee
  and a cent (`pk_core::exit::exit_floor`).
- **The limit never spends the edge** (`PK_MIN_NET_EDGE`, live 0.03). The
  limit is touch + `PK_LIMIT_OFFSET`, capped where buying and selling at
  Kalshi's price leaves less than 3¢ after both fees; an ask above the cap is
  no entry. On epoch 13's ten live entries the cap would have bound none (the
  0.20 gap and `PK_MIN_EDGE` keep Kalshi's price 16-41¢ over the ask): it is a
  rail for thin edges, not a change to today's fills.
- **Cash is not counted twice.** The wallet and the ledger see an exit's
  proceeds or a redemption at different times, and each reconciliation reset
  cash to whichever read came first (epoch 13: ±$9.64 and ±$10.32 pairs). A
  drift now waits while an order is in flight or a closed window awaits its
  resolution, and is applied once two reads agree (`reconcile` with
  `skipped`).
- **No shadow variants and no paper twin.** The owner removed them. They ran
  only in the observer, so the order path is unchanged; the observer does
  less. Live entries took ~225 ms decision → acknowledgement against ~43 ms
  for post-only probes on the same connection: the taker delay is the venue's.

**Epoch 13 (2026-10-14).** The epoch-12 review (−$24.31 live over 43 trades,
while the touch edge across all crossings was +9.8¢) found three mechanical
leaks, now closed:
- **Entries need a tradable edge.** Mid gaps on ZEC's ~0.5-wide Polymarket
  book led to buys above Kalshi's price. Entries on wide Kalshi books could
  never use the exit. Live now requires Kalshi spread ≤ 0.02, Polymarket
  spread ≤ 0.05, Kalshi's price − ask ≥ 0.10 and ask ≥ 0.20
  (`PK_MAX_KALSHI_SPREAD`, `PK_MAX_POLY_SPREAD`, `PK_MIN_EDGE`,
  `PK_MIN_ENTRY_PX`).
- **Exits that land.** The exit sells 3 ticks under the bid, never under
  entry + fee + 1¢ (`PK_ACTIVE_EXIT_TICKS`). A miss re-arms at once, or 1 s
  later when the shares were not credited yet.
- **Fixed size.** Every entry is 10 shares (`PK_ORDER_SHARES`). The 5-share
  minimum no longer refuses the dearer entries, which left mostly cheap tails.


**Epoch 11 (2026-10-12).** From the 10-10/11 tape (652 shadow entries replayed
through the books at 250 ms): threshold 0.20 (was 0.15), limit touch + 6¢ (was
+2¢), shares sized at the limit, the active exit (`pk_core::exit`), shadow
variants also scored at a realistic fill (`net_cents_real`), and the paper
latency learned from live taker orders only. Per decision the replay went from
about +1.0¢ to +2.9¢; its fill rates are optimistic (no rival takers).

The threshold was 0.15 in epochs 5–10 (from 2026-10-07). It was 0.10 in epochs
3 and 4, chosen because at the manual's 0.20 there were almost no fills. On the
Helix tape 0.15 beat 0.10 per day on price-side fills, in discovery and in the
holdout (research/PREREG.md C2); the `t10` shadow variant keeps measuring 0.10.
Because the first crossing decides, the threshold defines the trade, so this is
a different strategy from the manual's, with its own gates (below). Paper runs in Dublin, the closest location allowed to trade on
Polymarket; London is close-only and records only.

**Record, replay, paper, and live behind its guards.** `PK_EXECUTION_MODE=live`
places real orders (see "Live execution"). It is refused at startup without
`PK_LIVE_CONFIRM=yes`, which is set only once the paper gates pass, on a host in a
permitted jurisdiction. The UK, US, DE and FR are all close-only on Polymarket.

## Run

A Cargo workspace (Rust, edition 2024). Since 2026-10 it replaces the Python
service; results start at **epoch 7** (new journal and tape formats).

```bash
cargo build --release --workspace
cp .env.example.dev .env.dev   # fill in API_KEY and the Kalshi key; chmod 600
target/release/poly-kalshi-service --profile dev   # 127.0.0.1:8050 (starts pk-observer, :8052)
target/release/pk-status --profile dev             # 127.0.0.1:8051, read-only page
target/release/pk --help                           # order probe, netprobe, research
```

| Route | |
|---|---|
| `GET /health/ready` | both feeds up and every asset's current window identified |
| `GET /v1/obs/status` (observer, `:8052`) | the observer's lines, lag behind the trader, recorder, feed latencies, counters, PolyBolt |
| `GET /v1/series` (observer, `:8052`) | the per-second chart points (the trader samples none) |
| `GET /v1/status` | feeds, recorder, per-asset window and quotes, ledger, risk, order path (`X-API-Key`) |
| `GET /v1/recent` | last decisions, fills and settlements |
| `POST /v1/kill`, `POST /v1/kill/ack` | halt or resume paper entries (recording continues) |

Telegram `/kill` and `/status` run on a bot of its own (`PK_TELEGRAM_BOT_TOKEN`).
`pk-unit-alert` is the systemd `OnFailure=` hook (`infra/systemd`).

| Crate | |
|---|---|
| `pk-core` | books, fees, `decide()`, windows, ledger, risk, journal records |
| `pk-engine` | the line-driven engine (roles `full`, `trade`, `observe`; forward and reverse) |
| `pk-feeds` | Kalshi (RSA-PSS) and Polymarket (sharded) websockets, PolyBolt, the Binance/Kraken/Bitstamp references, window discovery |
| `pk-exec` | the Polymarket CLOB V2 order path: warm HTTP/2, L2 auth, EIP-712 signing, pre-signing, the probe |
| `pk-tape` | the zstd tape writer and the reader (it also reads the old `.gz` hours) |
| `pk-trader` | `poly-kalshi-service` and `pk-observer` |
| `pk-results`, `pk-status` | gates and results; the status page and `pk-unit-alert` |
| `pk-research`, `pk-cli` | the research code and the `pk` command |
| `pk-svc`, `pk-stats` | settings, alerts, logging; allocation-free histograms |

**Monitoring.** `pk-status` reads top to bottom:
1. **Alerts strip:** feeds down or stale, halts, crossed or missing books, unidentified windows, balance rejects, a paused recorder, stale data. Otherwise "all clear".
2. **Performance:**
   - *Capital & risk:* initial capital, cash, equity, realised and equity return %, capital in use, largest open exposure, max and current drawdown.
   - *Trade quality:* average win and loss, profit factor, expectancy, largest win and loss, longest losing streak, gross P&L and fees.
   - *Gates.*
3. **Live market:** the market state table (per asset bid/ask/mid, Δ against the threshold, what the rule would do now, book ages, this window's decision) and per-second Kalshi vs Polymarket charts.
4. **Edge:** the signal check (win rate vs Kalshi-implied vs price paid), the opportunity funnel (windows → crossings → skips → blocked → orders → fills → settled, total and today), and breakdowns by asset, by second and by entry band; the lead–lag table times each reference feed (Binance spot and perp, Kraken, Bitstamp) against Kalshi and Polymarket.
5. **History:** equity curve, daily results, the last 50 trades, recent engine events.
6. **System:** feeds, latency (engine queue delay next to the loop lag), the order path (decision → acknowledgement, pre-sign hit rate, assumed against measured paper latency), execution, and data quality.

- Results come from the journal on disk.
- Live state comes from one cached read of `/v1/status` every 5 s, so viewers add no load on the trader.
- **Export all data (.zip)** downloads the journal, events log, ledger, metrics summary, recording manifests and chart data, with no secrets.
- It runs at lower CPU priority and is published through a Cloudflare Tunnel with Access in front. See the poly-kalshi section of `infra/cloudflare/README.md`.

## Order path (the latency review's step 2)

`pk probe-orders` times the real order path without trading. With
`PK_ORDER_PROBE=on` and a funded `POLY_PRIVATE_KEY`, it posts a **post-only** BUY
of the minimum size `--back-ticks` (5) ticks below the best bid, and cancels it at
once:

- a guard refuses any price that is not strictly below both the bid and the
  ask, any notional above `--max-notional` ($3), and a second open order;
- a failed cancel is checked against the order's state, and a fill of any size
  stops the probe;
- every probe writes an `order_timing` record: decision → signed → written →
  headers → acknowledgement (monotonic ns) and the venue's own stamps, for the
  post and for the cancel, with the pre-sign hit or miss;
- `--cancel-all` cancels everything on the account and exits.

The transport is one warm HTTP/2 connection (pinned DNS refreshed off-path,
keep-alive pings, a replacement warmed before it is swapped in). Orders are
signed ahead of the decision when a gap nears the threshold, so the hot path is
a cache lookup and one POST. In the trader, the same exec bridge mirrors each
paper entry with a probe at the decision instant, and probes on a cadence
(`PK_PROBE_CADENCE_S`) between decisions.

**The paper venue's latency is measured, not configured.** `PK_PAPER_LATENCY_MS`
is only the seed. Once `PK_LATENCY_MIN_SAMPLES` round trips exist, the latency in
force is the p50 of the last `PK_LATENCY_WINDOW` decision → acknowledgement times.
It is held in memory, and each move of ≥ 1 ms goes to the engine as a `latency`
control line. So the tape records it, a replay reproduces it, and the journal has
a `latency` record. The status page shows the value in force and whether it is
still the seed.

## Live execution

`PK_EXECUTION_MODE=live` with `PK_LIVE_CONFIRM=yes`, a `POLY_PRIVATE_KEY`, a
`POLYGON_RPC_URL`, and one of two wallets:

- **An EOA** (`POLY_SIGNATURE_TYPE=0`): the key's own address holds the pUSD and
  has the CLOB token approvals, plus a little POL for the redemption gas.
- **A deposit wallet** (`POLY_SIGNATURE_TYPE=3`, `POLY_FUNDER` = its address):
  the wallet polymarket.com creates for a new account, which already has the
  approvals. Fund its deposit address. Orders are signed for it (EIP-1271), and
  settled positions are redeemed by a signed batch that Polymarket's relayer
  submits and pays for, so it needs a `POLY_RELAYER_API_KEY` (polymarket.com →
  Settings → API Keys) and no POL. At startup the service checks on chain that
  the key owns the wallet.

The dashboard's cash is the CLOB's balance for that wallet and signature type.
Funds at another address (for example the deposit wallet while
`POLY_SIGNATURE_TYPE=0`) read as zero, and every entry is refused as
`insufficient_cash`.

- **Entries.** Each decision is a FAK BUY at the decision's limit, rounded down to
  the tick and never above `PK_MAX_ENTRY_PX`. It takes what rests at or below the
  limit and the venue cancels the rest. Its size is the engine's, already cut to
  the depth at the limit with `PK_SIZE_TO_DEPTH=true`. The entry is refused under
  the token's minimum size, or above `PK_MAX_OPEN_NOTIONAL_USD`.
- **Results go through the engine.** The fill (from the acknowledgement, or by
  polling the order until it is no longer live) goes back as a `live_fill`
  control line. The engine opens the position and writes a `fill` record with
  `venue: "live"` in the paper schema, so `pk readout` and the gates read live
  fills unchanged. The paper venue simulates nothing in live.
- **Fast path** (`PK_FAST_PATH`, `pk_trader::fast`, `pk_trader::hot`). When an
  asset's gap nears the threshold the engine arms it (its Polymarket quotes, the
  rule's parameters, the entry per side); the order for each side is signed and
  built as HTTP/1.1 bytes ahead, with its L2 headers re-signed every 2 s. The
  Kalshi feed runs on a thread pinned to one core and busy-polling its socket;
  each frame updates its own copy of the Kalshi book and runs `decide()` on the
  armed quotes, and on an entry the bytes go out on a warm connection from that
  thread. The engine claims nothing twice: the trigger and the engine claim a
  window before sending, and the engine records the trigger's entry (`"fast":
  true`) when it reaches the frame. The reply goes through the usual result
  path. At start an empty signed `POST /order` checks that the venue accepts
  the fast path's headers. The status page shows frame → wire p50/p99.
- **Active exit** (`PK_ACTIVE_EXIT`). While a position is open the engine checks
  it on every update: once the held leg's best bid is within 1¢ of Kalshi's
  price for that side (Kalshi spread ≤ 2¢) and above the entry, it writes an
  `exit_signal` and the bridge sells with a FAK `PK_ACTIVE_EXIT_TICKS` (3) under
  the bid, never where both taker fees leave under 1¢ a share. What sold
  comes back as `live_exit` (`reason: "fair"`): an `exit` record, and a `settle`
  record with `outcome: "exit"` when nothing is left. When nothing sold, the
  bridge sends `exit_miss`. A killed or refused FAK re-arms at the next update,
  a balance reject (shares not credited yet) 1 s later, and after 8 misses a
  signal waits 5 s. An exit's balance reject never counts toward the
  balance-reject halt; under 1 share the remainder settles. The status page's Exits panel
  compares each exit with holding to settlement.
- **Fixed shares** (`PK_ORDER_SHARES`, epoch 13: 10). When it is set, every
  entry orders that many shares and the two sizes below only set the
  `too_small` check.
- **Compounding size.** The order size is `PK_ORDER_SIZE_PCT` of the balance (paper:
  ledger cash; live: the wallet's pUSD). It is held in memory and recomputed
  only at a reconciliation with no open position and no order in flight. Every
  change is a `sizing` record.
- **Reconciliation** runs every 30 s and after each fill or redemption:
  - it reads the wallet balance (a `balance` line: ledger cash is reset to it,
    any drift is a `reconcile` record, and the size is recomputed when flat);
  - it cancels and alerts on any resting order (entries are FAK, so nothing
    should rest);
  - it compares each open position with its token balance (`position_drift`);
  - it redeems settled winners on chain (a `redeem` record with the tx hash).
    `redeem_failed` is alerted after 20 tries; `redeem_no_effect` if the shares
    are still in the wallet; `gas_low` under 0.05 POL (EOA only: a deposit
    wallet's redemptions are gasless).
- **Risk.**
  - `PK_DAILY_LOSS_LIMIT_USD` halts on the ledger's realised P&L and on the
    wallet's equity drawdown since the day's first reconciliation.
  - Three balance/allowance rejects in a row halt.
  - Any halt (kill file, `POST /v1/kill`, Telegram `/kill`) also cancels every
    order on the account.
  - `POST /v1/flatten` or Telegram `/flatten` halts and sells every open
    position with a FAK at two ticks under the bid (`exit` records).
- **Startup.** A live order path that cannot connect (CLOB auth, Polygon RPC)
  exits the process non-zero, so `OnFailure` alerts. At startup the trader
  cancels everything and reads the wallet before its first entry. The ledger
  stores its mode, so a paper ledger is archived, never read as a live one.

`pk netprobe --minutes 1440` measures the warm round trip to the CLOB and Kalshi
from a host (the AWS-versus-Azure comparison in `infra/systemd/README.md`).

## How it works

```
                              trader process (poly-kalshi-service): the trade path only
Kalshi WS (orderbook_delta) ─┐
Polymarket WS (N shards) ────┼─▶ ingest: one clock, one stamp ──▶ channel ──▶ engine thread
control lines (windows,      │   (receive tasks)                             ├ Engine(role=trade)
  resolutions, reconnects) ──┘                                               ├ decide() on every update
                                                                             ├ PaperVenue (+latency, walk asks)
                                                                             ├ Ledger (cash; payout credited)
                                                                             ├ exec bridge → order probe
                                                                             └ ObserverLink: a non-blocking pipe
                              observer process (pk-observer, a child of the trader)
                               ├ the tape (hourly zstd files and manifests, its own thread)
                               ├ Engine(role=observe): markouts, series,
                               │   uncensored gap, window/resolution records, BBO checks, feed lags
                               ├ the journal (its only writer, in the trader's line order)
                               ├ PolyBolt reference prices (venue b, optional)
                               └ loopback API :8052 → pk-status
```

**Statistics never run on the trading loop.** The engine runs on one thread fed
by a channel from the feed tasks. The observer runs the same engine code in
`observe` role on the same lines, a few ms behind; a test checks that `trade` +
`observe` write exactly the journal of the one-process `full` engine, record for
record. The pipe drops market lines past 64 MB of backlog (the tape notes the
gap) but never journal records. If the observer dies, the trader keeps trading,
writes its own records to disk, alerts, and restarts it. `PK_OBSERVER=false`
restores the one-process layout.

- **Window identity** (`pk-core::window`, `pk-feeds::discovery`):
  - Polymarket's slug is `<asset>-updown-15m-<open_unix>`, with `clobTokenIds = [Up, Down]`.
  - Kalshi's series is `KX<ASSET>15M`; the market is the one whose `close_time = open + 900`.
  - The next window is discovered `PK_DISCOVER_LEAD_SECONDS` ahead and written to the tape.
- **Ghost gap** (kpc trap 3):
  - Books are keyed by the window's own ids, so an old window's book can't be read as the new one's.
  - Old books are dropped at the roll.
  - A leg counts only if it has updated since the window opened.
- **Decision** (`pk-core::decision`): manual §3 exactly, with three rules:
  - The first crossing decides.
  - A crossing above the cap consumes the window.
  - No exit, ever.
- **Stale and wide books** (the review's step 3): `PK_MAX_POLY_LAG_MS` and
  `PK_MAX_POLY_SPREAD` guard the live rule (off by default). The epoch-13
  gates are `PK_MAX_KALSHI_SPREAD`, `PK_MIN_EDGE` (Kalshi's price of the side
  minus the ask) and `PK_MIN_ENTRY_PX`; epoch 14 adds `PK_MIN_NET_EDGE` (the
  same after both taker fees, which also caps the limit). A spread, lag or
  edge gate that fails does not consume the window. A Polymarket book older than
  `PK_AGE_OUTLIER_MS` at a decision is journalled as an `age_outlier` with its
  cause: venue silence, a shard reconnect, or frame lag.
- **Paper fill** (`pk-engine::paper`):
  - The order meets the book `PK_PAPER_LATENCY_MS` after the decision.
  - It walks the asks up to the limit; partial fills are kept and the rest is cancelled.
  - Fees are computed per level from `feeSchedule`.
  - The naive touch fill is logged next to it for comparison.
- **Settlement** (`pk-core::ledger`):
  - The source is Polymarket's resolution: the `market_resolved` WS event, or a Gamma poll.
  - The payout is credited to cash (kpc trap 4).
  - Kalshi's result is recorded too, to measure venue disagreement.
- **Journal**: `data/state/<profile>/journal/YYYY-MM-DD.jsonl` holds every decision, fill, settlement, resolution, window summary and order timing.
- **Tape**: `data/raw/<profile>/<YYYYMMDD>/pk_<YYYYMMDD>_<HH>_<session>.zst`, one text line per frame (`<recv_ns> <venue> <raw>`), with a `.zst.json` manifest per hour.

**Parity.** Before the Python was deleted, its engine, results, tape research and
Helix studies were run on fixed tapes; the outputs are the goldens under
`crates/*/tests/golden/` (with the scripts that made them), and the Rust tests
match them field by field.

## Results epochs

Every journal record carries `epoch` (`PK_RESULTS_EPOCH`), `build` (the git commit) and `config` (a hash of the strategy parameters).
- The status page, `pk readout` and the export use only the current epoch, and the page header shows how many older records were set aside.
- Epoch 7 starts with the Rust service (`PK_RESULTS_EPOCH=7`, also the default).
- Changing the epoch archives the paper ledger (`ledger.epoch-<old>.json`) and starts a fresh one. So does switching between paper and live (`ledger.epoch-<old>.live.json` for a live one).
- With `PK_ORDER_SHARES` set, `config` hashes the share count instead of either size. Otherwise it hashes `PK_ORDER_SIZE_PCT` rather than the size, and the latency seed rather than the latency: those two move at runtime and are journaled as `sizing` and `latency` records.
- Bump the epoch whenever a fix invalidates earlier results.
- Records from before epochs existed, i.e. the ghost-level period, are always excluded.

## Research

```bash
pk replay run --record-dir data/raw/dev --out research/data/run1
pk replay compare --live data/state/dev/journal --replay research/data/run1/journal
pk nulls --run research/data/run1
pk readout --journal data/state/dev/journal
pk readout --journal data/state/dev/journal --exclude-assets BTC,SOL
pk fills run --record-dir data/raw/dev --out research/data/fills1 && pk fills grid --run research/data/fills1
pk inventory --record-dir data/raw/dev --journal data/state/dev/journal
pk acceptance --hours 24
```

- `replay run` re-runs the tape through the live engine. It writes a journal and a per-second `grid.parquet`; `--reverse` replays the reverse direction (Polymarket signals, Kalshi executes).
- `replay compare` must show 100% agreement with live on the same tape. A tape recorded with the order path on carries the measured latency as `latency` lines: pass `--tape-latency` to follow them as the trader did (`--latency-ms` holds until the first one); without it `--latency-ms` holds throughout.
- `nulls` is the manual's null battery. Expect `signal` ≈ +5 pp and `random_same_rule` ≈ 0 pp. Its random arms use a seeded generator of their own, not numpy's.
- `readout` reports the gates below, markouts, exits and capacity. `--exclude-assets` reads the journal as if those assets were never traded, next to the full read, net ¢ over the shares each settled position filled (PREREG parts 4 A1, 6 B1).
- `fills` decides once and fills many times (latency × limit offset × size) from each entry's ask ladder.

The research documents (`research/PREREG.md`, `research/RESULTS.md`) name the
Python modules they were written with: `research.<x>` is now `pk <x>`, and
`research.helix_<x>` / `research.helix.<x>` is `pk helix <x>`.

### The Helix tape (`~/kpc/raw`, 73 days of best bid/ask)

```bash
export HELIX_TAPE_DIR=~/kpc/raw
pk helix validate --day 20260920          # decoder checks (PLAN 0.1a)
pk helix resolutions                      # venue outcomes, regime, fee
pk helix replay --jobs 3                  # decide() over every window
pk helix inventory                        # day roles: discovery, holdout, earlier regime
pk helix sweeps --split discovery         # sweeps, nulls, latency (3.3)
pk helix leadlag --days 20260820,20260901 # who moves first (4.2)
pk helix fairvalue --split discovery      # Chainlink fair value against the gap (4.3)
```

`research/helix/README.md` documents the format, the clocks and what the
validation found. Only derived outputs are written (`research/data/helix/<day>/`);
the tape is memory-mapped and never copied.

## Gates (re-registered 2026-10-07 for the 0.10 strategy, before any new result was read)

The manual's magnitudes ($/entry +4.4 to +6.9, a +5 pp signal, the band shape)
were measured at 0.20 and do not apply to 0.10. They are still reported, never
gated. `pk-results` computes these; the status page and `pk readout` show them.

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
- Polymarket moved to CLOB V2 on 2026-04-28 (pUSD collateral, a new order struct). The order path signs with Polymarket's own Rust SDK (`polymarket_client_sdk_v2`), not the Helix adapter.
- Gamma returns 403 to library default user agents, so every request sends our own.
