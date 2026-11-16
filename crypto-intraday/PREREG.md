# Crypto intraday — kill test, pre-registration part 1

Written 2026-11-13, before any data was downloaded or any result produced. Rules are frozen;
changes go in a new part.

Sources:
- **PV**: Padyšák & Vojtko, *Seasonality, Trend-following, and Mean reversion in Bitcoin*
  (SSRN 4081000). Quantpedia's summary: long BTC 22:00 UTC, hold 2 h; 2015–2021; ~33 % a
  year, Sharpe 1.58, costs not reported.
- **SUW**: Shen, Urquhart & Wang, *Bitcoin Intraday Time-Series Momentum*, Financial Review
  57(2), 2022. The first half-hour return predicts the last half-hour return.

Prior evidence against: Baur et al. (FRL 2019) find no persistent time-of-day effects;
Schmidt & Kraft (SSRN 2026) find that after the spot ETFs, hour of day predicts volume and
volatility but not the direction of returns.

## Data

Binance USD-M perpetual 5-minute klines, BTCUSDT and ETHUSDT, monthly files from
data.binance.vision, 2022-01 → 2026-10 (after PV's sample; SUW's sample also ends earlier).
Files are checked against Binance's published SHA-256 checksums. `p(t)` = open of the 5-minute
bar starting at `t` (UTC); a trade with a missing price is skipped. All 7 days a week.

## Rules

| id | rule |
|---|---|
| R1 | long at p(22:00 UTC), exit at p(00:00 UTC next day) |
| R2 | r1 = p(00:30)/p(00:00) − 1 on UTC day D; long if r1 > 0, short if r1 < 0, flat if 0; enter p(23:30), exit p(00:00 D+1) |

## Costs (round trip, bp of entry price)

- **Taker 10 bp** (0.05 % per side, Binance USD-M VIP 0): base case.
- Maker 4 bp (0.02 % per side, assumes both sides fill passively at the quoted price):
  optimistic, reported.
- 0 bp: gross, reported.

## Tests (2022-01-01 → 2026-10-31)

One-sided t-test of mean net bp per trade > 0.

- **Primary**: R1 BTC at 10 bp.
- **Secondary family (Holm 5 %)**: R1 ETH at 10 bp; R1 BTC at 4 bp; R2 BTC at 10 bp; R2 ETH at 10 bp.

## Verdict

- **PASS**: primary p < 0.05 → shadow-trade R1 BTC with maker entries.
- **SECONDARY ONLY**: primary fails, at least one secondary survives Holm → paper-trade that
  rule only; new pre-registration before any change.
- **KILL**: neither.

## Description only (not gates)

- Per-year tables for R1 and R2.
- Mean return by UTC hour, before vs after the spot-ETF launch (2024-01-11).
- The ETF benchmark window: return 15:00 → 16:00 New York time and 16:00 → 17:00, weekdays.
- R2 on the US-equity clock (09:30–10:00 ET predicts 15:30–16:00 ET), weekdays.
