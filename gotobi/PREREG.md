# Gotobi — pre-registration, part 1

Written 2026-11-13, before any backtest output was produced. Rules below are frozen;
anything changed after seeing results goes in a new part with its own date.

Source: Bessho, Sugimoto & Suzuki, *Forex Trading Strategy That Might Be Executed Due
to the Popularity of Gotobi Anomaly*, arXiv 2301.13204 (EBS data, 2018–2020).

## Data

- HistData.com USDJPY M1 bars, `Data/USDJPY/DAT_ASCII_USDJPY_M1_*.csv`, 2020-01-01 to
  2026-09-30. Bid prices only.
- Stamps are New York wall-clock **with** DST (checked: Sunday open at 17:00 and the
  08:30 payrolls bar at 08:30 in both seasons), converted to JST.
- `p(t)` = close of the bar ending at minute `t`, forward-filled up to 5 minutes;
  a day missing any required price is dropped from that strategy.

## Calendar (paper's rules)

- Gotobi = 5th, 10th, 15th, 20th, 25th, 30th; a non-business date rolls back to the
  previous Japanese business day (weekends, `holidays.JP`, 31 Dec–3 Jan closed).
- Tuesday–Friday only, for Gotobi and control days.
- Control = every other Tuesday–Friday Japanese business day (all of them, not a
  random sample of equal size as in the paper).

## Strategies (no parameter is tuned)

| id | rule |
|---|---|
| H1 | long p(03:00) → p(09:55) |
| H1-GC3 | long if SMA25 crosses above SMA100 in [02:30, 03:00); fill one minute after the cross; exit p(09:55) |
| H2 | if p(09:00) > p(03:00): short p(09:55) → p(12:00) |
| COMBO | H1-GC3 + H2 on the same day (cost charged per leg) |

H1-GC with windows n = 1, 2, 4, 5 is reported for description only.

## Costs

Round trip **0.5 pip** (bid-only data: spread + slippage) in the base case; also reported
at 0.3, 1.0 and 2.0 pips.

## Periods

- **Replication, 2020** (the only year shared with the paper): descriptive. Expect the
  paper's sign (H1 > 0, H2 > 0 on Gotobi days). Too few days (~70) to be a gate.
- **Out of sample, 2021-01-01 → 2026-09-30**: the test.

## Tests (out of sample, base cost)

- **T1 (primary)**: H1 on Gotobi days, net mean > 0 with one-sided p < 0.05, **and** the
  Gotobi − control difference in gross H1 has one-sided Welch p < 0.05.
- **Secondary family, Holm at 5%**: H1-GC3 net > 0; H2 net > 0; COMBO net > 0;
  H2 Gotobi − H2 control (gross) > 0.

## Verdict

- **PASS**: T1 passes and H1 net mean is still > 0 at 1.0-pip cost → next step is an MT5
  demo forward test with the same rules.
- **SECONDARY ONLY**: T1 fails, at least one secondary survives Holm → paper trade that
  rule only, no live money, new pre-registration before any change.
- **KILL**: neither.

Per-year tables and the 2022–2024 intervention years are reported but are not gates.
