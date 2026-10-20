# Strategy specification

This document is the exact contract between the research report
([`deep-research-report-crossex.md`](deep-research-report-crossex.md)) and the
code. Where the report leaves a choice open, the choice is made here. Units:
`ppm` is parts per million of notional (1 bp = 100 ppm).

## Instruments

USDT perpetuals that exist on both Binance USDⓈ-M and Bybit V5 linear. They
must have the same contract unit, so no multiplier translation is done. Names
can differ, e.g. `SHIB:1000SHIBUSDT:SHIB1000USDT`. Metadata comes from Binance
`/fapi/v1/exchangeInfo` (`PRICE_FILTER.tickSize`, `LOT_SIZE.stepSize/minQty`,
`MIN_NOTIONAL.notional`) and Bybit `/v5/market/instruments-info`. Both legs
trade the same quantity. Quantity steps are the least common multiple of the
two venue steps, and the minimum quantity is the larger venue minimum rounded
up to that step. The first time both books are verified, the two venue mids
must agree within `CXA_MAX_MID_DIVERGENCE_PPM` (default 1%). Otherwise the
symbol is disabled for the run.

## Books

- Binance: REST snapshot (`limit=500`), then `@depth@0ms` diffs from the
  `/public` route. The first diff bridges if `U ≤ id+1 ≤ u` **or**
  `pu == id`; after that every diff must chain via `pu`. Any break, or a diff
  with more than 512 levels on a side, marks the book `gap` and triggers a
  fresh snapshot (at most once per second per symbol).
- Bybit: `orderbook.50` (20 ms). A snapshot (or `u == 1`) replaces the book.
  Deltas must have `u == previous u + 1`. A gap triggers an
  unsubscribe/subscribe, which makes Bybit push a new snapshot.
- Books keep the best 512 levels per side. Deeper levels are truncated.

## Basis and edge

For a symbol, let `m_B` and `m_Y` be the Binance and Bybit mids and
`m = (m_B + m_Y)/2`.

- Mid basis `b = (m_Y − m_B) / m`.
- Mean basis `b̄`: an EWMA of `b`, sampled at most once per second, half-life
  `CXA_BASIS_HALFLIFE_MS` (30 min). Entries are blocked until the EWMA has
  `CXA_BASIS_WARMUP_MS` (10 min) of history.
- Direction A (`dir = +1`) is long Binance and short Bybit. Direction B
  (`dir = −1`) is the reverse. Direction-signed values: `β = dir·b` and
  `β̄ = dir·b̄`.

For trade quantity `q` (`CXA_TRADE_NOTIONAL / m`, rounded down to the common
step), sweep full displayed depth:

- entry gap `g = (VWAP_sell(short venue, q) − VWAP_buy(long venue, q)) / m`
- exit gap now `x = (VWAP_buy(short venue, q) − VWAP_sell(long venue, q)) / m`
- expected capture `c = g − β̄ − (x − β)`. This is the entry gap minus the
  expected exit gap once the basis returns to its mean, where the expected exit
  gap is the mean plus today's crossing cost at this size.
- fees `f = 2·(taker_B + taker_Y)`, using the account's real rates in shadow
  with keys.
- funding cost `F` over `CXA_EXPECTED_HOLD_MS` (10 min). For every venue whose
  next settlement falls inside that window, add the long leg's rate and
  subtract the short leg's rate.
- net `n = c − f − F − CXA_SAFETY_PPM`.

**Entry:** when the pair is flat, take the direction with the larger `n`, and
enter if `n ≥ CXA_MIN_NET_PPM` (1000 = 10 bp) and every risk gate passes.

## Exit triggers (checked on every book update while open, in order)

1. Kill or halt.
2. Converged: `β − β̄_entry ≤ CXA_EXIT_BAND_PPM` (2 bp). `β̄` is frozen at
   entry, so EWMA drift does not count as convergence.
3. Stop: `g_entry − x − f ≤ −CXA_PAIR_STOP_PPM` (150 bp).
4. Max hold: `CXA_MAX_HOLD_MS` (30 min).
5. Funding: a settlement within `CXA_FUNDING_GUARD_MS` (2 min) would cost
   more than `CXA_FUNDING_TOLERANCE_PPM` (1 bp).

## Orders (shadow and future live)

Every leg is an IOC limit order.

- Entry limit: the worst level touched by the decision-time sweep, widened by
  `(n − min_net)/2`. A fill at that limit still clears the threshold.
- Exit limit: the worst swept level widened by `CXA_CLOSE_SLIPPAGE_PPM` (50 bp).
- Repair limit: best price widened by `CXA_HEDGE_SLIPPAGE_PPM` (2%).

**Repairs.** After both entry legs resolve, any residual (from unequal fills)
is unwound on the venue holding the excess. If nothing filled on one side, the
pair ends `legged_out` once flat. When a close leaves any leg open, it is
re-closed with repair limits. After `max_repair_attempts` (20), the pair is
marked out at the book and the engine halts until restart.

## Shadow fill model

- A leg submitted at `t` arrives at `t + L_venue`. `L_venue` is half a signed
  REST round trip, sampled from the last 256 measurements (`sampled`), their
  p95 (`pessimistic`), or a configured constant (`fixed`), plus
  `CXA_ENGINE_PENALTY_NS` (1 ms). Draws use a seeded PRNG, so replay is
  deterministic.
- On arrival the leg sweeps the book as it stood at the arrival time, before
  the event that moved the clock past it is applied. Only
  `CXA_AVAILABLE_PPM` (50%) of each displayed level counts. The fill is
  rounded down to the venue step, and the fee is the venue's taker rate on the
  filled notional.
- If the book is not verified at arrival, the IOC fills nothing.
- Known optimism: shadow fills don't consume the book, so a repair right after
  an entry on the same venue sees the same liquidity. Signed REST GETs don't
  include matching-engine time; the penalty term covers that until Phase 2
  canaries measure send→fill directly.

## Funding accounting

When a venue's `nextFundingTime` advances past the previous value, the
settlement is paid on the position held at that moment:
`−qty · mark · rate_before_settlement` (longs pay positive rates).

## Census

On every evaluation, the raw executable gap `g` is computed for $250, $1k,
$5k and $25k in both directions. An episode starts when `g` crosses 30, 40, 50
or 75 bp and ends when it falls back, giving
`O sym size dir threshold start duration peak`. This is the report's
"executable opportunity frequency / persistence" dataset, and it is collected
whether or not trades fire.

## Risk gates (entry only)

Each gate maps to a risk code:

| Gate | Code |
|---|---|
| Not halted (kill file or loss halt) | `halted` |
| Symbol enabled | `symbol_disabled` |
| Each venue feed's last message ≤ `CXA_MAX_FEED_AGE_NS` (1 s) | `feed_stale` |
| Each book updated within `CXA_MAX_BOOK_AGE_NS` (60 s) | `book_stale` |
| Taker fee known on both venues | `fees_unknown` |
| Funding rate and next time known on both venues | `funding_unknown` |
| Basis warm | `basis_cold` |
| Open pairs < `CXA_MAX_OPEN_PAIRS` (3) | `max_pairs` |
| Notional ≤ `CXA_MAX_PAIR_NOTIONAL` | `notional_limit` |
| Symbol cooldown `CXA_COOLDOWN_MS` (5 s) elapsed since the last cycle | `cooldown` |

Cumulative net ≤ −`CXA_DAILY_LOSS` sets a loss halt, which only a restart
clears. The service restarts daily, so the limit applies per day.
