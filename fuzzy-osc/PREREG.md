# Fuzzy oscillator vote — kill test, pre-registration part 1

Written 2026-11-13, before any backtest output. Rules are frozen; changes go in a new part.

Source: Shabani, Nasiri & Nafardi, *Forex Trading Robot Using Fuzzy Logic*, arXiv 2507.06383
(EUR/USD, Jan–Jun 2022, MT5 tester, in-sample only).

The paper does not give the timeframe, exits, position size, costs, membership-function
breakpoints or 33 of its 36 rules. This is therefore **our** fixed version of the idea, not a
replication. A KILL means "the oscillator-vote idea has no edge in a reasonable form", not
"the paper's numbers are false".

## Data

HistData M1 bid bars (New York time with DST), resampled to **H1** (primary) and **M15**
(secondary). AUDUSD, USDJPY, XAUUSD, 2020-01 → 2026-09 (no EURUSD on hand). XAUUSD
2009–2019 is reported as an extra out-of-period check, not a gate.

## Signal

Indicators per paper: RSI(9, 14, 21) (Wilder), CCI(9, 14, 21) (typical price),
Stochastic slow %K with (K, D, slowing) = (5, 3, 3), (14, 7, 7), (21, 14, 14).

Membership per input (trapezoids, linear between the breakpoints):

| indicator | buy = 1 at ≤ | buy = 0 at ≥ | sell = 0 at ≤ | sell = 1 at ≥ |
|---|---|---|---|---|
| RSI | 25 | 35 | 65 | 75 |
| CCI | −125 | −75 | 75 | 125 |
| Stoch | 15 | 25 | 75 | 85 |

neutral = 1 − max(buy, sell). Per indicator system (Mamdani, AND = min across the three
periods): b = min μ_buy, s = min μ_sell, n = min μ_neutral. Defuzzified
res = (0.2·b + 0.5·n + 0.8·s)/(b + n + s), neutral if all three are 0.
Signal per paper: RSI/CCI buy if res < 0.4, sell if res > 0.6; Stoch buy if res < 0.2, sell if res > 0.8.

**Vote**: long if ≥ 2 of 3 systems say buy and none says sell; short mirror; else flat.
Variant **CRISP** (diagnostic): same vote with each system = "all three periods past the
classic level" (RSI 30/70, CCI ±100, Stoch 20/80).

## Trading

- Enter at the next bar's open after the signal bar closes; one position per instrument;
  signals during a position are ignored.
- **Exit after a fixed holding period**: primary 12 bars on H1; also 4 and 24 bars.
- Round-trip cost: AUDUSD 0.5 pip, USDJPY 0.5 pip, XAUUSD $0.30.
- Return per trade in basis points of entry price, net of cost.

## Gate (H1, hold 12, FUZZY, 2020-01 → 2026-09)

**PASS** only if both:
1. Pooled across the three instruments, net mean bps/trade > 0 with t ≥ 2.
2. At least 2 of 3 instruments have net mean > 0.

Otherwise **KILL**. Other holds, M15, CRISP and the reversed (momentum) sign are reported for
description only; a reversed-sign result is a new hypothesis, not a pass.
