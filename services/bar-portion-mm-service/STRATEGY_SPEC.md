# PMM-BP strategy specification v1

This document separates behavior stated in Stoikov et al., *Market Making in
Crypto* (SSRN 5066176) from the choices needed to turn the incomplete research
description into a deterministic Binance USD-M shadow service.

## Paper-defined behavior

- Input bars are one-minute OHLC candles.
- `BP[t] = (close[t] - open[t]) / (high[t] - low[t])`, in `[-1, 1]`.
- BP has a contrarian relationship with the following minute's return.
- The directional study fits one-dimensional rolling linear regression on 36
  days (51,840 minute observations) and evaluates the following nine days.
- PMM uses two quote levels. Optimized start and step spreads were roughly four
  to five times monthly volatility across the paper's asset universe.
- Triple-barrier controls include stop loss, take profit, and a time limit.
- Historical research used Binance data. The 24-hour live comparison used OKX
  perpetuals on SOL-USDT, DOGE-USDT, and GALA-USDT.

The paper does not publish the PMM-BP controller, full live configs, regression
target, forecast-to-reference mapping, or exact per-asset barriers.

## Project-defined behavior

All items below are conservative, configurable v1 choices rather than claims
about the unpublished Hummingbot implementation.

### Bar and regression alignment

- Only a kline with Binance `x=true` is accepted. Repeated or older close times
  are ignored. An in-progress candle never affects a quote.
- A zero-range candle (`high == low`) has BP `0`. A malformed candle with
  `high < low` is rejected.
- The research target paired with `BP[t]` is
  `r[t+1] = close[t+1] / close[t] - 1`. The pair enters calibration only after
  candle `t+1` closes, preventing lookahead.
- Rolling OLS includes an intercept and the latest 51,840 completed `(BP, r)`
  pairs. Research evaluation freezes a fit for the next 12,959 observations,
  matching the count reported by the paper. Runtime accepts a versioned fit;
  its provenance belongs in the run manifest.

### Reference price and quotes

- `forecast = clamp(intercept + slope * BP, -5 bp, +5 bp)`.
- `inventory_ratio = clamp(net_base / max_inventory, -1, +1)`.
- `inventory_skew = inventory_ratio * 3 bp`.
- `reference = mid * (1 + forecast - inventory_skew)`.
- Two quotes are placed on each side. Per-side distances begin at 4 bp and step
  by 2 bp. A 20-candle EWMA of absolute close-to-close minute returns creates a
  volatility floor of four times that estimate. This is a responsive proxy,
  not the paper's unspecified monthly-volatility implementation.
- Bids round down and asks round up to the tick. A quote that would take
  liquidity is moved one tick behind the opposite touch.
- Default size is 0.01 base units per level. Prices are replaced after a
  one-tick change or five seconds. Accepted quotes alone update active state.
- Entry quotes are post-only. Shadow entry never assumes a touch fill.

### Queue and costs

- A resting shadow quote begins behind the displayed quantity at its price.
- Opposite aggressor trades consume queue ahead before filling the quote. A
  print through fills the remainder. Unexplained book-size reductions do not
  improve queue position, which is intentionally pessimistic.
- Default one-way decision-to-venue latency is 5 ms, maker fee is 2 bp, and a
  barrier market close pays 5 bp plus visible-touch slippage. Runs must replace
  these with measured/account values and retain 1x/2x variants.

### Risk and exits

- Default maximum net inventory is 10 base units, maximum order notional is
  1,000 USDT, maximum open orders is eight, and maximum entry rate is eight per
  second. Projected inventory is checked before each quote.
- Default stop loss and take profit are both 20 bp from average entry. The time
  barrier is five minutes. The paper reports no stable volatility relationship
  for these values, so they require forward validation.
- Trailing exits are disabled in v1; introducing one changes the paper-adjacent
  barrier semantics and requires a separately versioned replay experiment.
- The default post-exit cooldown is eight minutes. This borrows the conservative
  edge of the paper's separate 8-9 minute PEPE PMM-Simple result; it is not a
  published PMM-BP parameter.
- One-second aggressive-trade imbalance blocks the side exposed to adverse
  selection beyond an absolute 0.8 threshold.
- New quotes stop within 60 seconds of funding, on books older than 250 ms, or
  when spread exceeds 20 bp. Daily loss defaults to 50 USDT and peak drawdown
  to 75 USDT on 10,000 USDT shadow equity.
- Feed gaps, crossed/empty books, durable kill state, journal failure, stale
  data, or unknown state cancel entries and fail closed. A sequence-gap pause
  clears only after snapshot synchronization; a durable kill requires operator
  acknowledgement after its file is removed.

## Golden vectors

1. `O=100, H=110, L=90, C=105` gives `BP=5/20=0.25`.
2. `O=H=L=C=100` gives `BP=0` by project rule.
3. OLS observations `(-1, 0.01), (0, 0), (1, -0.01)` give intercept `0` and
   slope `-0.01`.
4. With `mid=100`, forecast `-1 bp`, zero inventory, and 4 bp distance,
   reference is `99.99`, level-one raw bid/ask are `99.950004` and `100.029996`;
   at a `0.01` tick they become `99.95` and `100.03`.
5. With inventory at its positive maximum, zero forecast, and 3 bp skew,
   `mid=100` produces reference `99.97`.
6. A long entered at `100` hits the 20 bp stop at `99.80` and the take profit
   at `100.20`. Equality fires the barrier.

The executable tests encode these rules. Any semantic change requires a spec
version change, updated vectors, and replay comparison.
