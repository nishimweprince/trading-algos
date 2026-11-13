# fx-fix-flows

Out-of-sample test (2020-01 → 2026-09, HistData M1 USDJPY and AUDUSD) of two flow-based
intraday FX effects. Research only, no order entry.

- **Fix reversals**: Krohn, Mueller & Whelan, *Foreign Exchange Fixings and Returns Around the
  Clock*, Journal of Finance 79(1), 2024. USD rises into the Tokyo/ECB/London fixes and falls after.
- **Local-session depreciation**: Breedon & Ranaldo, *Intraday Patterns in FX Returns and Order
  Flow*, JMCB 45(5), 2013.

Rules, costs and gates: [PREREG.md](PREREG.md). Output: [results/RESULTS.md](results/RESULTS.md).

```bash
cd fx-fix-flows && uv sync && uv run pytest && uv run fx-fix-flows --out results
```

`--data-root` overrides `/Users/pnishimwe/Documents/Markets/Data` (expects `<SYM>/DAT_ASCII_<SYM>_M1_*.csv`).

## Verdict (part 1, 2026-11-13): KILL

Net of 0.5 pip per leg, bp per day:

| rule | days | net bp/day | t | gross bp/day |
|---|---|---|---|---|
| T-USDJPY (Tokyo reversal, primary) | 1,373 | +0.38 | 0.45 | +1.14 |
| T-AUDUSD (primary) | 1,375 | −0.21 | −0.20 | +1.25 |
| E-USDJPY (ECB/London) | 1,643 | +0.18 | 0.19 | +0.94 |
| E-AUDUSD | 1,639 | −3.07 | −2.67 | −1.61 |
| W (all four legs) USDJPY / AUDUSD | ~1,300 | +0.30 / −2.70 | 0.22 / −1.68 | |
| BR sessions USDJPY / AUDUSD | ~1,645 | −1.89 / −2.70 | −1.61 / −1.92 | |

- Nothing survives Holm in either family; T is negative on both pairs at a 1.0-pip cost.
- The gross Tokyo reversal is ~1.2 bp/day, against ~4.8 bp/day for JPY in KMW's 1999–2019
  sample (Table I: −4.06% and +7.94% a year). The effect has shrunk by about three quarters,
  and the clean W shape is not visible in the 2020–26 intraday path (`results/usd_path.png`).
- The one leg with the paper's sign and t > 2 is AUDUSD post-Tokyo (+1.9 bp, t = 2.8), but its
  pre-Tokyo leg has the wrong sign, and AUDUSD post-London runs against the paper (t = −3.2).
- T-USDJPY earned in 2021–22 (t ≈ 2.1 each) and lost in 2023 (t = −2.8): regime, not edge.
- Breedon–Ranaldo was already net negative for these two pairs in their own sample; it still is.

Caveat: the pre-Tokyo leg needs a price at exactly 17:00 ET, which does not exist on Sunday
(the market opens then), so Mondays drop out of T and W (~1,375 vs ~1,750 days). That follows the
pre-registered missing-price rule and was not changed after seeing results.
