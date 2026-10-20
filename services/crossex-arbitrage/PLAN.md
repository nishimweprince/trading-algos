# Plan: Binance ↔ Bybit cross-exchange perp arbitrage (C++)

## Context

`services/crossex-arbitrage/deep-research-report-crossex.md` concludes that pre-funded Binance–Bybit arbitrage is
*conditionally* viable. It works only with USDT perps on both venues, taker/taker IOC, low leverage, and a
measured executable basis that clears about 25–35 bp all-in (21 bp fees + slippage + funding). That number has
to be measured, not assumed. So we build a C++ service that:

- **Phase 1:** runs on mainnet with **shadow fills only**: live books, real account fee tiers, measured
  signed-request RTT, and latency-delayed fills that sweep depth. No orders are sent, and there is no devnet/testnet.
- **Phase 2:** switches the same strategy path to live taker/taker IOC execution with a small canary,
  but only after a pre-registered gate passes.

Decisions made with the user:
- Perp vs perp (Binance USDⓈ-M against Bybit V5 linear USDT). Both accounts have futures access.
- Liquid alt basket of about 10–20 common USDT perps, plus BTC/ETH as controls.
- AWS Tokyo (ap-northeast-1), confirmed by a Phase 0 probe from both regions. Binance is the price leader
  and is matched in Tokyo; Bybit, which lags, is matched in Singapore. The Binance-move→Bybit-order path is
  about 35 ms one-way from either region, so Tokyo wins on the Binance hedge leg and on existing infrastructure.
- Read-only API keys in Phase 1, used for real commission rates, balances and signed-RTT probes.
- Phase 2 canary: about $1–2k per venue, $100–250 matched notional per pair.
- Exit: on convergence, with a max-hold timer and a forced close before an adverse funding settlement.
- Standalone CMake project that copies and adapts `bar-portion-mm-service` code, with no shared library.
  Code layout has no runtime cost, because everything compiles into one binary either way.
- Phase 2 execution: taker/taker IOC only. Maker/taker is out of scope.

## Location and conventions

- Work on branch `feat/crossex-arbitrage`. The project lives in `services/crossex-arbitrage/` (the report and
  transcript are already there, untracked). The namespace is `cxa` and the binary is `crossex`.
- Mirror `services/bar-portion-mm-service/`: C++20, CMake ≥3.25 + Ninja presets
  (release/sanitizers), libcurl (wss) + OpenSSL + pthreads, `-Wall -Wextra -Wconversion -Wshadow -Werror`,
  and one `tests/test_main.cpp` plus fixtures, with CLI subcommands registered as ctest tests.
- Copy and adapt these files from bpmm:
  - `include/bpmm/types.hpp` (fixed-point `Decimal`, 1e8 scale, `mul_scaled`, `round_up/down`)
  - `spsc.hpp`
  - `book.hpp/.cpp`: generalise to a venue-agnostic L2 book, and add `vwap(side, qty)` / `sweep(side, qty, limit)`
  - `src/binance.cpp`: the libcurl `CURLOPT_CONNECT_ONLY=2` + `curl_ws_recv` loop, REST snapshot, ping/pong
  - `journal.cpp`: JSONL record types, replay, gapcheck
  - `status.cpp` + `dashboard.cpp`: a 127.0.0.1 status server with `/api/summary` and `/api/activity`
  - `risk.hpp`: `RiskCode`/`RiskLimits` pattern; durable kill file; the `--mode live` refusal test
- Docs, matching bpmm: `README.md`, `STRATEGY_SPEC.md` (exact formulas/thresholds), `PREREG.md` (go/no-go),
  `RUNBOOK.md`, `docs/aws-tokyo.md`, and `infra/systemd/crossex-shadow.service`.

## Architecture (threads)

```
binance-md thread ─┐ (curl ws, combined stream, all symbols)        SPSC
bybit-md thread  ──┼──────────────────────────────────────────────▶ decision thread
private/probe thr ─┘ (signed REST: fees, balances, RTT samples)       (books, opportunity, risk,
                                                                       Venue: Shadow | Live, positions)
recorder thread ◀── bounded SPSC (raw + normalised + decisions + fills) ──┘
control thread: status server, kill file, metrics (reads atomics/snapshots only)
```
As in bpmm, an overflow on any queue or a journal write failure halts the pipeline.

## Modules (`include/cxa/*.hpp`, `src/*.cpp`)

| Module | Responsibility |
|---|---|
| `instruments` | Load Binance `GET /fapi/v1/exchangeInfo` and Bybit `GET /v5/market/instruments-info?category=linear`; derive tick/step/minQty/minNotional per venue. Basket comes from config, with an explicit symbol map and multiplier (e.g. `1000PEPEUSDT`). At startup, check that the two venues' mids agree within 1%, or refuse the symbol. |
| `binance_md` | Combined stream `<s>@depth@0ms` + `<s>@markPrice@1s` for every symbol on one connection. Per-symbol REST snapshot and the bpmm `U/u/pu` sync logic. Resnapshot a symbol on gap. |
| `bybit_md` | `wss://stream.bybit.com/v5/public/linear`, `orderbook.50.<S>` (20 ms) + `tickers.<S>` (funding rate, nextFundingTime). On `type=snapshot` (or `u==1`), replace state; apply deltas and check `u` continuity. Send `{"op":"ping"}` every 20 s. |
| `book` | Venue-agnostic fixed-point L2 book (from bpmm), with `state()`, freshness, `sweep()` returning VWAP + filled qty within a limit. |
| `fees_funding` | Binance `GET /fapi/v1/commissionRate`, Bybit `GET /v5/account/fee-rate?category=linear`, refreshed hourly. Fall back to configured rates only if refusing to start is disabled. Live funding rate and next timestamp per venue; funding history is pulled for the research report. |
| `opportunity` | For each symbol and each configured size (default $250 / $1k / $5k / $25k), compute `g_exec` from depth-swept VWAPs in both directions. Net edge = `g_exec − fees(real) − slippage_buffer − E[funding differential over expected hold] − safety`. Emits entry/exit intents. Writes a **census** record (gap distribution and persistence per symbol and size) whether or not a trade fires. |
| `risk` | Gates: book verified + fresh (both venues), symbol notional cap, gross/net delta cap, max concurrent pairs, funding gate, venue-health (reconnects, gaps, signed-request errors), daily loss / drawdown kill, durable kill file. |
| `venue` | `Venue` interface: `submit_pair(PairIntent)`, `on_market(...)`, `fills()`. `ShadowVenue` in Phase 1, `LiveVenue` in Phase 2. The strategy and risk path stays identical. |
| `shadow` | Fill model (below). |
| `position` | Per-symbol legs (qty, avg price, fees, funding accrued), consolidated delta, realised/marked P&L with the attribution split from the report: quoted alpha, staleness, impact, legging, fees, funding. |
| `probe` | Signed RTT sampler: a persistent keep-alive HTTPS connection sends a signed GET (`/fapi/v2/positionRisk`, `/v5/position/list`) every N s and keeps a p50/p95/p99 histogram per venue. Also records feed age (local receive − exchange ts). |
| `keys` | Load secrets from env/file, never logged. In shadow mode, refuse to start unless the keys are confirmed read-only: Binance `GET /sapi/v1/account/apiRestrictions` (no futures trading, no withdrawals) and Bybit `GET /v5/user/query-api` (`readOnly=1`). |
| `journal` / `report` | JSONL records: S/R/D (book), M (mark/funding), O (census), I (intent), F (shadow/live fill), P (position), L (latency sample). `crossex replay` (deterministic state hash), `gapcheck`, `daily-report`, `census-report`. |

## Shadow fill model (Phase 1 core; this is what makes shadow honest)

1. When an intent is created at `t_dec`, each leg becomes a pending IOC limit. Its limit is the worst price
   that still clears the edge threshold.
2. It "arrives" at `t_dec + L_venue`, where `L_venue` is drawn from that venue's **measured** signed-RTT/2
   distribution plus a configurable matching-engine penalty. A `--latency pessimistic` mode uses p95.
3. On arrival, sweep **the book as it stands at that moment**. The leg fills when the first book event with
   `receive_ns ≥ arrival` has been applied. A partial fill comes from depth running out within the limit.
   A configurable haircut on displayed size (default 50%) models other takers.
4. Unequal fills leave a residual. A simulated hedge order then goes through the same path: a taker on the
   lagging leg, or an unwind of the excess. Each such event is counted as a legging event.
5. Fees use the real commission rates. Funding is accrued at each real settlement timestamp using the live
   rates observed for that venue.
6. Exit runs the same path in reverse. Triggers: exit `g_exec` ≤ `exit_bp`, `max_hold` timer, or a
   settlement within `funding_guard` with an adverse differential.

## Phases and milestones

**M0 – scaffold + probe.** Set up the CMake project, copied core types, `crossex probe --duration 24h` (feed age
and signed RTT for both venues), and the read-only key check. Run the probe once from Tokyo and once from
Singapore and record the result in `docs/aws-tokyo.md`. The host is confirmed by p99, not p50.

**M1 – market data.** Instrument mapper, both books with gap detection and resync, funding streams, journal
R/D/M. Tests: Binance `pu` chain, Bybit snapshot/delta/`u==1` reset, symbol multiplier, gapcheck on fixtures.

**M2 – opportunity + census.** Size-swept `g_exec`, fee/funding engine, census records,
`census-report` (frequency of >30/40/50/75 bp at each size, plus persistence at 5/50/500 ms and 5 s). This
already answers "do the gaps exist?" before any fills are simulated.

**M3 – shadow execution.** ShadowVenue, positions, exit logic, residual hedge, risk gates, kill file,
`--mode live` refusal (ctest `WILL_FAIL`), `drill` (gap, stale feed, one-leg, kill), `benchmark`
(decision-path p99, zero allocations).

**M4 – deploy shadow.** Status server and dashboard (open pairs, census, P&L attribution, latency), systemd
unit, RUNBOOK. Deploy to Tokyo and run for ≥14 days. Commit `PREREG.md` **before** the data is collected.

**Gate (PREREG.md, frozen before M4 starts).** Measured under **pessimistic** latency, all of these must hold
to proceed: ≥14 days and ≥150 shadow cycles; mean net ≥ 8 bp per cycle; bootstrap 95% CI lower bound > 0;
legging-event rate ≤ 10%; worst cycle loss ≤ 20× mean net cycle P&L. A failure ends the project there. We do
not re-tune thresholds on the same data.

**M5 – Phase 2 live (taker/taker IOC).**
- Order entry uses the WS order APIs to keep latency low: Binance `wss://ws-fapi.binance.com/ws-fapi/v1`
  `order.place`, and Bybit `wss://stream.bybit.com/v5/trade` `order.create`. REST (`POST /fapi/v1/order`,
  `POST /v5/order/create`) is the fallback and the query/cancel path.
- Private streams: the Binance listenKey user-data stream (`ORDER_TRADE_UPDATE`, `ACCOUNT_UPDATE`, keepalive),
  and Bybit `v5/private` with `order`, `execution.fast`, `position`, `wallet`.
- OMS state machine as in the report: client IDs derived from the decision ID, and `UNKNOWN` → query by
  client ID before any retry. The residual hedger works from **actual** executed qty.
- Startup enforces one-way position mode, isolated margin and 2× leverage on every basket symbol through each
  venue's API, and refuses to trade on any mismatch. REST reconciliation runs at startup, after every private
  reconnect, every 60 s, after an UNKNOWN and after a hedge; a mismatch freezes the symbol.
- Separate trade keys: withdrawals disabled, IP-whitelisted to the Tokyo host.
- `--mode live` needs all of: config `live.enabled=true`, an env acknowledgement token, no kill file, and a
  clean reconciliation.

**M6 – canary.** $100–250 per pair on 2–3 basket symbols. Calibrate the shadow model's latency and haircut
against real send→ack→fill timings and compare shadow-vs-live P&L for the same decisions. Each size step
up must pass the same PREREG criteria, measured on live data.

## Verification

- `cmake --preset release && cmake --build --preset release && ctest --preset release`, and the same under
  `sanitizers`. Tests cover decimal parsing, both book reconstructions, VWAP sweep, fee and funding sign
  conventions, shadow latency scheduling and partial fills, OMS transitions (M5), and live refusal.
- `crossex replay tests/fixtures/session.jsonl` gives the same state hash on every run.
- Run `crossex shadow --symbols BTCUSDT,ETHUSDT --journal data/x.jsonl --port 8070` for 10 minutes on the VM,
  not the laptop. Check `/api/summary`, check that `gapcheck` is clean, and check that `census-report` prints
  distributions.
- Phase 2 dry check: start `crossex live` without the acknowledgement token or with the kill file present,
  and confirm it exits before any order socket opens. The canary's first trade is supervised manually.
