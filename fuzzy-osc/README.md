# fuzzy-osc

Kill test of the fuzzy RSI/CCI/Stochastic vote from Shabani, Nasiri & Nafardi,
*Forex Trading Robot Using Fuzzy Logic* (arXiv 2507.06383). Research only.

The paper leaves out the timeframe, exits, sizing, costs, membership breakpoints and 33 of
its 36 rules, so [PREREG.md](PREREG.md) fixes a reasonable version of the idea before any
results were seen. [results/RESULTS.md](results/RESULTS.md) is the generated output.

```bash
cd fuzzy-osc && uv sync && uv run pytest && uv run fuzzy-osc --out results
```

Data: HistData M1 (`DAT_ASCII_<SYM>_M1_*.csv`) under `/Users/pnishimwe/Documents/Markets/Data/<SYM>/`,
override with `--data-root`.

## Verdict (part 1, 2026-11-13): KILL

H1, 12-bar hold, fuzzy vote, net of 0.5 pip / $0.30 round trip, 2020-01 → 2026-09:

| symbol | trades | net bps/trade | t |
|---|---|---|---|
| AUDUSD | 940 | +1.9 | 1.18 |
| USDJPY | 989 | −4.1 | −2.73 |
| XAUUSD | 938 | −4.9 | −1.73 |
| pooled | 2,867 | −2.4 | −2.01 |

- Gate needed pooled t ≥ +2 and 2 of 3 instruments positive; it got t = −2.0 and 1 of 3.
- Every H1 and M15 hold (4/12/24 bars), fuzzy or crisp, loses pooled. Fuzzy is not better
  than the plain-threshold version.
- Gold 2009–2019 with the same rules: −3.3 bps/trade (t = −1.7).
- 2022, the paper's test year, is the only positive year (+2.6 bps, t = 0.8).
- The reversed (momentum) sign is at most +3.1 bps (t = 1.6, H1/24) and negative on M15:
  not a lead worth a new pre-registration.
