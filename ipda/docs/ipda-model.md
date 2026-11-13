# IPDA model (`STRATEGY=ipda`)

`file.txt` calls its Supertrend arrows "IPDA", and the live default (`STRATEGY=reversal`) trades
its RSI "Buy/Sell Chance" labels. Neither uses the ICT **Interbank Price Delivery Algorithm**
ideas: data ranges, premium/discount, liquidity, killzones, displacement into a fair value gap.
`src/ipda_model.py` implements those, built only from definitions already in `file.txt`
(fractal BOS/ChoCH from Section 1, the FVG test from Section 2).

The same class drives the live service and the backtest (`backtest/run_ipda.py`), so the two
cannot drift.

## Rules (v1, fixed 2026-11-13 before any backtest of this model)

All clock times are `America/New_York`. A trading day runs 17:00 → 17:00 (gold's daily break).

1. **IPDA data range.** Over the last `IPDA_RANGE_DAYS` (20) *completed* trading days, take the
   highest high and lowest low. Equilibrium (EQ) is the midpoint. Below EQ is discount, above
   is premium.
2. **Liquidity pools.** Previous trading day high/low (PDH/PDL), and the Asian range: the
   high/low of 20:00–24:00 at the start of the trading day.
3. **Killzones.** London 02:00–05:00 and NY AM 07:00–10:00. Sweep, structure shift and order
   all belong to one killzone; at most one setup per killzone.
4. **Sweep.** Inside the killzone a 1M low trades below min(Asian low, PDL) (sell-side) or a 1M
   high trades above max(Asian high, PDH) (buy-side). The extreme is tracked until the shift.
5. **Market structure shift.** On closed 3M bars, the Pine fractal (`ms_length` 5): after a
   sell-side sweep, a close crossing above the latest unbroken swing-high fractal (bullish
   BOS/ChoCH); the mirror after a buy-side sweep.
6. **Displacement FVG.** The newest FVG in the shift's direction formed on a 3M bar at or after
   the sweep bar, by `file.txt`'s test: bullish `low > high[2] and close[1] > high[2]`, bearish
   mirrored. No FVG means no setup.
7. **Premium/discount filter** (`IPDA_PD_FILTER`, on). The FVG midpoint must be in discount of
   the data range for a long and in premium for a short.
8. **Order.** Limit at the FVG midpoint (consequent encroachment). Stop at the sweep extreme ∓
   `IPDA_STOP_BUFFER_PIPS` (0). Target at `IPDA_RISK_REWARD` (2.0) × risk. The risk must lie
   within `IPDA_MIN_RISK_PIPS` and `IPDA_MAX_RISK_PIPS` (10–150 pips, $1–$15 on gold). The order
   expires at the killzone end (GTD). The model never cancels early, so live and backtest agree.

## Pre-registered evaluation

Primary hypothesis: v1 at defaults has positive expectancy after a $0.20 spread on XAUUSD.
In-sample is 2009-03 → 2019-12 and out-of-sample is 2020-01 → latest. **Pass** requires OOS
expectancy > 0 R with the 95% CI lower bound > −0.05 R, and IS expectancy > 0 R. Variants
(PD filter off, liquidity target, single killzone) are exploratory and cannot rescue a fail.

Backtest fill model: a buy limit fills when ask (bid + spread) ≤ entry, a sell limit when bid ≥
entry, at the limit price (or the open if it gaps through). If the fill minute also touches the
stop, it counts as a stop. Afterwards, a minute touching both levels counts as a stop.

## Live wiring

* `STRATEGY=ipda` sends `execution_type=limit` with `entry_price`, absolute `stop_loss` and
  `take_profit`, and `expires_at` = killzone end.
* At startup the model is seeded with `IPDA_RANGE_DAYS + 5` D1 candles from market-data-service.
  That assumes the broker's D1 rolls at 17:00 New York, as FTMO and most MT5 gold feeds do.
* `DATA_LOOKBACK` must be ≥ 1440 so the warm-up covers the Asian range.
* The break-even advisory does not track IPDA orders: a pending limit has no fill to track.
* Set `TRADING_SESSIONS=` (empty) or `london_kz,ny_am_kz`. The killzones already gate inside
  the model.

## Result (2026-11-13): v1 FAILS

`backtest/run_ipda.py`, XAUUSD M1 2009-03 → 2026-09, $0.20 spread:

| Variant | Orders | Filled | Exp R | IS (2009–19) | OOS (2020+) |
| --- | --- | --- | --- | --- | --- |
| **ipda_v1 (pre-registered)** | 1,645 | 1,019 | −0.136 | −0.137 [−0.247, −0.026] | −0.136 [−0.267, −0.005] |
| no_pd_filter | 2,474 | 1,483 | −0.081 | −0.077 | −0.088 |
| liquidity_target | 1,633 | 1,013 | −0.119 | −0.107 | −0.135 |
| london_only | 604 | 352 | −0.199 | −0.224 | −0.168 |
| ny_am_only | 1,041 | 667 | −0.103 | −0.092 | −0.118 |
| rr_1_5 | 1,645 | 1,019 | −0.112 | −0.136 | −0.079 |

Gross (zero spread) v1 is −0.043R, the win rate is 28.9% against the 33.3% a 2R target
needs, and 16 of 18 years are negative. The premium/discount filter *hurts* (−0.136 → −0.081R
without it). Do not enable `STRATEGY=ipda` on a funded account on this evidence.
