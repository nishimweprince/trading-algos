# Crypto trend rotation — pre-registration, part 1

Written 2026-11-13, before any data was downloaded or any result produced. Rules are frozen;
changes go in a new part.

Source: Zarattini, Pagani & Barbon, *Catching Crypto Trends: A Tactical Approach for Bitcoin
and Altcoins*, SFI Research Paper 25-80 / SSRN 5209907 (revised 2025-04-09). Reported, net of
0.10 % costs, Jan 2015 → mid-Mar 2025: ~18 % CAGR, Sharpe > 1.5, ~11 % a year alpha vs BTC.
The full paper was not accessible (SSRN 403); the rules below come from the abstract and
secondary summaries (CXO Advisory, Concretum). Where those are ambiguous, the choice made here
is marked **(ours)**.

## Data

Binance **spot** USDT pairs, daily klines (UTC), from data.binance.vision monthly archives,
2017-08 → 2026-10. Delisted pairs are included (the archive keeps them), so the universe is
survivorship-free on Binance. Files are checked against the archive's MD5 ETags.
Excluded bases: stablecoins and fiat (USDC, BUSD, TUSD, USDP, PAX, DAI, FDUSD, USDD, AEUR,
EUR, GBP, AUD, TRY, BRL, RUB, UAH, NGN, ZAR, BIDR, IDRT, BVND, PLN, RON, ARS, JPY, MXN, COP,
CZK, EURI, XUSD, USD1, RLUSD, USDE, PYUSD), gold tokens (PAXG, XAUT), and Binance leveraged
tokens (bases ending in UP, DOWN, BULL, BEAR).

## Universe (monthly)

On the first day of each month, eligible = pairs with ≥ 365 days of history and a median daily
quote volume ≥ $2 M over the previous 30 days. Members = the top 20 eligible by that median
(**ours**: ranking by median quote volume, refreshed monthly). A coin that drops out of the
universe is closed at that day's close.

## Signal (per coin, per lookback L ∈ {5, 10, 20, 30, 60, 90, 150, 250, 360})

Daily closes `C`. `HI_L(t)` / `LO_L(t)` = max / min of closes over the L days ending t−1.
- Flat → long when `C(t) > HI_L(t)`; the stop starts at `(HI_L(t) + LO_L(t)) / 2`.
- Long: `stop(t) = max(stop(t−1), (HI_L(t) + LO_L(t)) / 2)`; exit when `C(t) < stop(t−1)`.

## Sizing

Each (coin, lookback) slot gets capital 1 / (20 × 9) **(ours: fixed slots, not "divide by the
number of open positions")**. An open slot holds `min(0.25 / σ90, 2)` × its capital, where σ90
is the annualised (√365) standard deviation of the coin's last 90 daily returns. Coin weight =
sum over its open slots. Long-only; unused capital earns 0.

## Execution and costs

Weights computed from closes up to day t earn the close-to-close return t → t+1. Every change
in weight (including the daily volatility resizing) pays **0.10 %** of the traded notional
(base); also reported at 0.25 % and 0.05 %.

## Periods

- **Replication**: 2019-01-01 → 2025-03-14 (inside the paper's sample; tests whether the
  result reproduces on Binance data and our reading of the rules).
- **Out of sample**: 2025-03-15 → 2026-10-31 (~19.5 months, after the paper).

## Verdict (net 0.10 %)

- **KILL** if the replication-window Sharpe is < 1.0 (the published result does not reproduce).
- **PASS** if it reproduces **and** the out-of-sample Sharpe > 0 **and** the out-of-sample
  alpha vs BTC buy-and-hold (OLS of daily returns on BTC, annualised intercept) > 0
  → forward paper-trade.
- **HOLD** if it reproduces but out of sample fails either condition → no trading; re-check
  after six more months of data under these same rules.

Reported for description only: BTC-only ensemble, the cost sensitivity, per-year returns,
max drawdown, average gross exposure and turnover.
