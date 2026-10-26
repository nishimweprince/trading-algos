# poly-kalshi-service

The isolated Predict.fun and Hyperliquid HIP-4 adapters live in `crates/pk-predict`,
`crates/pk-hip4`, and `crates/pk-venue-core`. They do not sit on the existing
trader hot path. Deployment, promotion gates, kill behavior, and Tokyo host
setup are documented in [`docs/TOKYO_VENUE_ADAPTERS.md`](docs/TOKYO_VENUE_ADAPTERS.md).

The kpc convergence trade, rebuilt to measure its one untested repair. On 15-minute
crypto up/down binaries, when Kalshi's YES mid and Polymarket's differ by at least
0.20, buy the cheap Polymarket leg as a taker, at or below 0.60, and sell it once
Polymarket's bid catches up with Kalshi's price (the active exit), else hold to
settlement. Kalshi is the signal only; Binance, Kraken, Bitstamp and Coinbase are recorded
and compared (lead–lag panel) but never decide: from Dublin Binance arrives after
both venues move, and the others are being measured.

**On-chain SOL (Helius LaserStream, `PK_LASERSTREAM`, off by default).** The
observer can also record the Orca and Raydium SOL/USDC pools (0.04%) and Pyth's
SOL/USD push feed over LaserStream gRPC from Helius London, at processed
commitment (venue `h`, one compact line per account write; feed lag from the
Helius node's `created_at`). Each account is its own lead–lag source. It is a
measurement, never traded. On 2026-10-23, after 9 h, Kalshi moved before every CEX feed
(the Bitstamp shocks 84% of the time), so the bar is high. **Decision rule** after 2–3 days
including US hours: an on-chain source is a candidate signal only if Kalshi and
Polymarket both answer it with a p50 lag ≥ +50 ms and ≤ 30% of answers before it;
otherwise record a no and turn it off. Polymarket settles on Chainlink, not on
any of these (see below).

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
| `pk-feeds` | Kalshi (RSA-PSS) and Polymarket (sharded) websockets, PolyBolt, the Binance/Kraken/Bitstamp/Coinbase references, Helius LaserStream (gRPC, on-chain SOL), window discovery |
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
4. **Edge:** the signal check (win rate vs Kalshi-implied vs price paid), the opportunity funnel (windows → crossings → skips → blocked → orders → fills → settled, total and today), and breakdowns by asset, by second and by entry band; the lead–lag table times each reference feed (Binance spot and perp, Kraken, Bitstamp, Coinbase, and on-chain SOL when LaserStream is on) against Kalshi and Polymarket.
5. **History:** equity curve, daily results, the last 50 trades, recent engine events.
6. **System:** feeds, latency (engine queue delay next to the loop lag), the order path (decision → acknowledgement, pre-sign hit rate, assumed against measured paper latency), execution, and data quality.

- Results come from the journal on disk.
- Live state comes from one cached read of `/v1/status` every 5 s, so viewers add no load on the trader.
- **Export all data (.zip)** downloads the journal, events log, ledger, metrics summary, recording manifests and chart data, with no secrets.
- It runs at lower CPU priority and is published through a Cloudflare Tunnel with Access in front. See the poly-kalshi section of `infra/cloudflare/README.md`.

## pk-limitless: Kalshi → Limitless (a process of its own)

The same rule, with Limitless's 15-minute BTC and ETH Up/Down markets as the leg.
These mirror Polymarket's (`isPolyArbitrage`) and settle on the same Chainlink 60 s TWAP.
The research behind it: Kalshi's move is worth ≤ 1 bp on spot, so spot can't carry it,
but another binary can.

- **Isolation.** `pk-limitless` (`infra/systemd/pk-limitless.service`) shares nothing
  with the trader:
  - it opens its own Kalshi socket and the Limitless Socket.IO socket;
  - state lives in `data/state/<profile>/limitless`, the tape in `<PK_RECORD_DIR>-limitless`
    (venues `k`, `l`, `c`);
  - its API is on `127.0.0.1:8053`;
  - the unit is `CPUAffinity=0-2`, `Nice=5`, so it never runs on the trader's hot core.

  Starting, stopping or crashing it never touches the Polymarket trader.
- **The 500 ms taker hold.** Limitless holds every taker order (FAK/FOK, a crossing GTC)
  for the market's `takerDelayMs` (500 ms), and makers can cancel meanwhile. Paper
  entries and exits therefore fill against the book at decision + measured round trip +
  taker delay. The fill at the decision's book is journaled beside it, so `delay_cost`
  measures what the hold costs.
- **Fee.** 5% × (1 − p) of the USDC value, i.e. `0.05 · p(1 − p)` per share: the
  Polymarket formula at rate 0.05. Makers pay nothing.
- **Live** needs `PK_LIM_EXECUTION_MODE=live` and `PK_LIM_LIVE_CONFIRM=yes`, plus:
  - a scoped API token for the signing EOA (`LIMITLESS_API_KEY/SECRET`; the key defaults
    to `POLY_PRIVATE_KEY`), with the profile in `eoa` trading mode;
  - USDC and a little ETH on Base;
  - `BASE_RPC_URL`;
  - the one-time `pk-limitless --profile dev approve`.

  Orders are FAK at the limit, with a dead-man heartbeat. Results come from
  `/orders/status/batch`. Positions are redeemed on-chain after resolution, because
  Limitless never redeems for an EOA.
- **Gate before live:** ≥ 150 delayed-fill entries over ≥ 3 days including US hours,
  net ¢/share > 0 at t ≥ 2, median 5 s markout ≥ −3¢.
- **Latency probes** (the status page's Limitless tab, `/limitless`):
  - warm HTTP/2 round trip on the uncached order-book route (cached answers are rejected),
    plus the cold request, TCP connect and Cloudflare colo;
  - the Socket.IO subscribe → ack time and server ping interval;
  - per-asset feed lag (venue stamp → receive) and frame interval;
  - Kalshi → Limitless response lag;
  - `decide()` µs;
  - live taker ack and ack → final;
  - `pk-limitless --profile dev probe-orders`: post-only buys far below the bid,
    cancelled at once.

- **SOL hourly (paper only, `PK_LIMH_*`, on whenever `HELIUS_API_KEY` is set).**
  Limitless has no SOL 15-minute market since 2026-07-08; its "Solana Up or Down
  Hourly" (a Polymarket mirror) resolves on Binance SOL/USDT's 1 h candle (close
  ≥ open). It is priced from Helius on-chain SOL:
  `P(Up) = Φ(ln(S/open)/(σ√τ))`, where S is the freshest of Pyth, Orca and Raydium
  times its Binance basis (5-minute EWMA). Pyth's push account updates only about every 10 s; the pools about once a second.
  σ is the EWMA of 1 s on-chain returns. Entries need the ask ≥ 10¢ under fair and
  pass the same net-edge, price and 10¢-spread gates, once an hour, with delayed paper
  fills. The Binance-only fair value is journaled beside it (`fair_up_binance`), as is
  a Brier score of the fair value at entry. State is in `limitless/sol-hourly`.
  This opens a second Helius LaserStream subscription (the observer has the other),
  which counts against the same Helius credits.

- **Maker arm (paper, `PK_LIMM_ENABLED`; `crates/pk-limitless/src/maker/`).** The
  10-23/24 review found the taker side adversely selected. In every missed entry
  the book repriced 7–74¢ within the 500 ms hold. The fills that remained lost
  (−13.7¢/share), and with instant fills the Kalshi signal earned −2.6¢ ± 8.1.
  Resting post-only orders and cancels are never held, so this arm quotes
  instead:
  - **Markets:** every Limitless crypto mirror of Polymarket in `PK_LIMM_SERIES`.
    The default is BTC/ETH 15-min, and BTC, ETH, SOL, XRP, BNB and DOGE hourly
    and daily. They come from `/markets/active`, with Polymarket's Up/Down
    tokens from Gamma via `metadata.externalSlug`.
  - **Fair value:** Polymarket's Up mid (same rules, same oracle), when its
    spread is ≤ 0.04.
  - **Quotes:** buy YES at `fair − h − skew` and buy NO at `1 − (fair + h − skew)`.
    `h` is 0.02, capped at the market's `maxSpread`, and `skew` leans against
    inventory (`PK_LIMM_MAX_POS` 30). Quotes stay post-only and stop 60 s,
    300 s or 1800 s before a 15-minute, hourly or daily close.
  - **Pauses:** a 2¢ Kalshi or Polymarket jump in 1 s pulls both quotes for
    1.5 s.
  - **Fills** come from Limitless's own trade prints (`/markets/<slug>/events`,
    polled every 2 s or 10 s), matched against the quote that was live at the
    print's time. A print through our price fills us; at our price, the queue
    ahead trades first.
  - **Inventory:** YES + NO pairs are locked at $1, and the net settles on
    Limitless's outcome. Makers pay no fee; the rebate is estimated, not
    counted.
  - **Measured:** spread captured, 5 s / 30 s markouts against fair, uptime,
    and net ¢ per share.
  - **Gate before live:** ≥ 300 fills over ≥ 3 days including US hours, net ¢/share
    > 0 at t ≥ 2, 30 s markout better than −h, uptime ≥ 80%.
  - **Flow (10-24, 24 h):** BTC 15-min 466 prints / 4,327 shares; ETH 240 /
    1,709; hourly and daily a few dollars each.

| Journal record (`limitless/journal`) | |
|---|---|
| `lim_window` | a window identified on both venues |
| `lim_decision` | each consumed crossing, with the instant fill it would have had |
| `lim_fill`, `lim_no_fill`, `lim_exit` | what the held order got (`held_ms`, `instant_*`) |
| `lim_settle`, `lim_redeem` | resolution and on-chain redemption |
| `lim_resp` | one Kalshi ≥ 2¢ move and Limitless's answer |
| `lim_probe` | the minute's network-probe summary |
| `lim_live_order` | a live order's timing and result |

## pk-okx: Kalshi → OKX event contracts (a process of its own, paper)

The same rule, with OKX's 15-minute BTC, ETH and SOL Up/Down event contracts
(`<ASSET>-UPDOWN-15MIN`) as the leg. Their windows open and close at Kalshi's
instants. They settle on OKX's own `<ASSET>-USDT` index: the last-60 s average
against the index at the open (`stk`). Kalshi settles on BRTI. The venue survey
behind it is in `research/VENUES.md`.

- **Host.** The Tokyo VM, in crossex's place. OKX moved its matching from Hong
  Kong to Tokyo in July 2026. From Tokyo, OKX is ~18 ms (Cloudflare NRT) and
  Kalshi's origin is ~153 ms; from Dublin they are ~250 ms and ~88 ms.
  Deploying there stopped crossex. Its data, `/etc/crossex-hl.env` and its
  binary are kept. To go back:
  `sudo systemctl disable --now pk-okx pk-status && sudo systemctl enable --now crossex-hl-shadow crossex-hl-compress.timer`.
  Both serve 127.0.0.1:8070, so only one can run at a time.
- **Isolation.** `pk-okx` (`infra/systemd/pk-okx.service`) shares nothing:
  - its own Kalshi socket and OKX public socket (`books`, `trades`, `index-tickers`);
  - state `data/state/<profile>/okx`, tape `<PK_RECORD_DIR>-okx` (venues `k`, `o`, `c`);
  - its API on `127.0.0.1:8054`.
- **Books.** OKX publishes UP only; DOWN is its mirror. A snapshot comes
  first, then absolute-size updates chained by `seqId`/`prevSeqId`. On a break,
  the book is dropped and the feed resubscribes for a fresh snapshot
  (`okx_seq_breaks`, `seq_gaps`).
- **Paper fills** land at decision + the measured REST round trip (the warm
  probe's p50; OKX holds no taker order), against the book as it is then. An
  entry the touch cannot fill in full is partial (`entries_partial`), and the
  fill rate is a gate figure.
- **Fees are assumptions** until `check` reads the account's:
  `PK_OKX_FEE_RATE · (p(1 − p))^PK_OKX_FEE_EXPONENT` per share for a taker, and
  `PK_OKX_SETTLE_FEE` per winning share at settlement. Every fill journals its
  gross and fee apart, so a corrected fee re-scores the journal without a re-run.
- **Outcomes.** OKX has no public settlement route for event contracts. Once a
  window expires, its candles end with a row at `expTime`, without volume,
  priced 1 (Up won) or 0 (Down won). Each window's `okx_outcome` holds:
  - OKX's result;
  - Kalshi's `result`;
  - the outcome OKX's recorded index implies.

  How often they disagree is on the page.
- **`pk-okx --profile dev check`** is the go/no-go for the account on this host.
  It reads `account/config`, the `EVENTS` trade fee and the USDT balance, then:
  1. posts one post-only buy at the lowest tick on this BTC window (guards:
     below the bid, never crossing, ≤ $1);
  2. cancels it at once;
  3. confirms nothing filled.

  `--no-order` reads only. A refusal prints OKX's code.
- **No live mode.** `PK_OKX_EXECUTION_MODE=live` is refused: there is no order
  path yet.
- **Gate before any live work:** ≥ 150 delayed-fill entries over ≥ 3 days
  including US hours, net ¢/share > 0 at t ≥ 2, median 5 s markout ≥ −3¢. Report
  with it:
  - the fill rate;
  - Kalshi → OKX response lag;
  - the settlement disagreement.
- **Page:** `pk-status` with `PK_OKX_ENABLED=true` serves `/okx`. On a host that
  runs only `pk-okx`, `PK_STATUS_HOME=okx` serves it at `/` and skips the trader.
  On Tokyo it listens on 8070, which the tunnel routes as
  `crossex.nishimweprince.dev`.

| Journal record (`okx/journal`) | |
|---|---|
| `okx_window` | a window identified on both venues, with OKX's strike |
| `okx_decision` | each consumed crossing, with the instant fill it would have had |
| `okx_fill`, `okx_no_fill`, `okx_exit` | what the delayed order got (`held_ms`, `instant_*`, `gross`, `fee`) |
| `okx_outcome` | OKX's and Kalshi's results, and the one OKX's index implies |
| `okx_settle` | a paper position settled, with the settlement fee |
| `okx_resp` | one Kalshi ≥ 2¢ move and OKX's answer |
| `okx_probe` | the minute's network-probe summary |

## pk-polymaker: Kalshi-priced maker on Polymarket (a process of its own)

**Retired (2026-10-26).** The live maker is now `services/pk-maker-cpp`
(`pkm`), a C++ process with the same rule and defaults (the `PK_PM_*`
settings) that re-quotes on every book update instead of every 50 ms and
reads fills from the user channel. Do not run both on one wallet. The
cutover, including carrying `polymaker/state.json` over, is in that
service's RUNBOOK. The section below describes the Rust implementation.

The taker fills only about a quarter of its entries: Polymarket holds each
taker order ~210 ms, and its makers pull the good trades in that time. Resting
orders and cancels are not held. `pk-polymaker` therefore quotes instead of
taking. The 4.5-day tape replay of this rule (2026-10-25) made +1.00¢ ± 0.41
per share filled at h = 0.02; at h = 0.05 it lost.

- **Quotes**, per coin, on the current 15-minute Up/Down market. The fair
  price is q = Kalshi's YES mid (spread ≤ 0.02, book ≤ 10 s old).
  - Buy Up at `floor(q − h − skew)` and buy Down at `floor(1 − (q + h − skew))`,
    in 1¢ ticks, post-only GTC.
  - `skew = PK_PM_SKEW_K · net / PK_PM_MAX_NET`, net = Up − Down shares.
  - Each bid stays below its token's best ask, within 0.05–0.95.
- **Pulled or not quoted:**
  - a Kalshi mid move of ≥ 2¢ within 1 s, which pauses both sides for 1.5 s;
  - the window's first 20 s and last 60 s;
  - a feed silent for 5 s or reconnected;
  - the `KILL` file, which also sends a cancel-all;
  - a side whose fill would take |net| past 20, or open cost past $60.
- **Requotes.** Kalshi's mid flickers across a cent, so a new target must
  hold before a bid moves. A bid above its target is cancelled after 200 ms
  (`PK_PM_ADVERSE_MS`). One below its target is raised after 300 ms, once the
  gap is ≥ 1¢. Jumps of 2¢ pull at once (the pause). Posts are capped at 8 per
  second.
- **First legs.** A bid that opens or adds to a one-sided position is quoted
  only in the window's first 5 min (`PK_PM_OPEN_LEG_S`) and while Kalshi's fair
  is within 0.15–0.85 (`PK_PM_OPEN_MIN_FAIR`/`MAX_FAIR`). On 2026-10-26 a
  second leg took ~200 s (p50) to come; windows whose first fill came after
  minute 5 paired 11–20% of the time and lost $23.92, those before paired 59%
  and made $14.24. Bids that pair held shares run until the stop.
- **Pair guard.** With shares held on one side at average cost c, the other
  side bids at most `1 − c − PK_PM_PAIR_MARGIN` (0.02), so every completed
  Up + Down pair locks at least 2¢ a share.
- **Markouts** against Kalshi's mid: 5 s, 30 s, 2 min and 5 min after each fill
  (the last two stop at the close), and at the close itself. They separate
  adverse selection from what settlement adds.
- **Fills** come from the venue: every second, `size_matched` of each order
  (`/data/orders`, then `/data/order/{id}` once it is gone). Makers pay no fee.
  - Every 30 s, each held token's wallet balance is checked against the booked
    inventory. Three checks in a row with the wallet short halt the maker.
  - Up + Down pairs pay $1; the net settles on Polymarket's resolution, and
    winners are redeemed (relayer for a deposit wallet).
- **Cash.** A post refused for "not enough balance" carries the venue's free
  balance. For 3 s only bids it covers are sent, so a wallet waiting on
  Polymarket's redemptions after a close holds its bids instead of firing
  rejects (`balance_rejects`, `cash_waits`).
- **Risk.** Realised P&L today at −$15 latches a halt (persisted), which lasts
  until the next UTC day or `touch data/state/<profile>/polymaker/ACK`.
  Cancel-all runs at start, on halt and on stop; a quote loop stalled for 2 s
  triggers cancel-all too.
- **Modes.**
  - `PK_PM_MODE=shadow` (the default) decides and journals every order and
    sends none.
  - `live` needs `PK_PM_LIVE_CONFIRM=yes`. It is refused while
    `PK_EXECUTION_MODE=live`, because the taker and the maker share the wallet.
    Pause the taker (`paper`) first.
- **Isolation.** `infra/systemd/pk-polymaker.service`: state
  `data/state/<profile>/polymaker`, API `127.0.0.1:8055` (`/v1/pm/status`,
  `/v1/pm/series`).
- **Page:** `pk-status` with `PK_PM_ENABLED=true` serves `/polymaker`, which is
  the "Maker" tab on the main page. `PK_STATUS_HOME=polymaker` serves it at `/`
  and stops polling the taker (Dublin since 2026-10-26, with the taker stopped).

| Journal record (`polymaker/journal`) | |
|---|---|
| `pm_window` | a window found on both venues |
| `pm_order` | post, reject, cancel, cancel_failed |
| `pm_fill` | shares matched, with the fair at fill, the capture, the net and the order's age |
| `pm_markout` | Kalshi's mid 30 s after a fill against its price |
| `pm_settle` | a window's inventory settled: pairs locked, P&L |
| `pm_redeem` | a redemption's transaction, or its error |
| `pm_drift` | the wallet's token balance differing from the booked inventory |
| `pm_halt`, `pm_ack`, `pm_kill` | the risk latch and the kill file |

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
