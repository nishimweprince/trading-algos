# gotobi

Backtest of the USD/JPY **Gotobi anomaly**: on the 5th/10th/…/30th of the month,
Japanese importers buy dollars at the 09:55 JST TTM fix, and USD/JPY tends to drift up
into 09:55 and give it back afterwards. Source: Bessho, Sugimoto & Suzuki,
arXiv 2301.13204 (EBS data 2018–2020). Research only, no order entry.

- [PREREG.md](PREREG.md) — frozen rules, costs, periods and pass/kill gates.
- [results/RESULTS.md](results/RESULTS.md) — generated tables, verdict and charts.

## Run

```bash
cd gotobi
uv sync
uv run pytest
uv run gotobi --data-dir /Users/pnishimwe/Documents/Markets/Data/USDJPY --out results
```

`GOTOBI_DATA_DIR` overrides the default data path. Input is HistData.com
`DAT_ASCII_USDJPY_M1_*.csv` (yearly or monthly files; the `.txt` gap reports are ignored).

## Layout

| module | role |
|---|---|
| `data.py` | load M1 bars (New York time with DST → JST), minute price grid |
| `jpcal.py` | Japanese business days, Gotobi roll-back, Tue–Fri filter |
| `strategy.py` | per-day H1 / H1-GC(n) / H2 / COMBO outcomes in gross pips; event paths |
| `stats.py` | PF/PR/W, t-tests, Welch, Holm, block bootstrap |
| `report.py` | runs both periods, applies the PREREG verdict, writes results |

## Caveats

- Bid-only bars: spread is a flat round-trip cost (0.5 pip base, 0.3–2.0 sensitivity).
- HistData is a retail aggregate, not EBS; the 09:55 fix move should still show up.
- The only year shared with the paper is 2020; 2021–2026 is genuinely out of sample.

## Verdict (part 1, 2026-11-13): KILL

Out of sample 2021-01 → 2026-09, 344 Gotobi days, 0.5-pip round trip:

| rule | net mean pips | t | PREREG test |
|---|---|---|---|
| H1 03:00→09:55 long | +2.2 | 1.43 | T1 fails (p = 0.077); Gotobi − control only +1.6 pips, t = 0.77 |
| H1-GC3 | +1.5 (N = 60) | 0.39 | fails |
| H2 09:55→12:00 short | +2.6 | 1.77 | p = 0.038, fails Holm |
| COMBO | +2.7 | 1.58 | fails |

What is still there, but too small to trade as the paper specifies:

- The shape of the anomaly survives: Gotobi days rise ~6 pips from 00:00 to 09:55 JST
  (~3.5 of it before the 03:00 entry) and drop ~4 pips in the 20 minutes after the fix.
  Against a daily H1 standard deviation of ~29 pips that is noise for a single trade.
- The 09:55→09:56 drop is the one strongly significant effect: −1.6 pips on Gotobi
  days vs −0.4 on control (difference −1.2, t = −4.9). It is about one spread wide.
- Edge was concentrated in 2021–22; 2023–25 H1 was flat to negative.
- H1-GC2 (+10.4 pips, t = 3.6, N = 52) is one of five windows tried; treat as noise.

Any of these is a new hypothesis and needs a PREREG part 2 before it is tested.
