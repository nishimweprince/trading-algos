# crypto-trend

Out-of-sample test of the rotational Donchian-ensemble trend strategy from Zarattini, Pagani &
Barbon, *Catching Crypto Trends: A Tactical Approach for Bitcoin and Altcoins* (SFI 25-80,
SSRN 5209907). Binance spot USDT pairs, daily, survivorship-free (delisted pairs included).
Research only, no order entry.

Rules, costs and gates: [PREREG.md](PREREG.md). Output: [results/RESULTS.md](results/RESULTS.md).

```bash
cd crypto-trend && uv sync
uv run python -m crypto_trend.fetch     # ~105 MB, 27k monthly zips, MD5-checked
uv run pytest && uv run crypto-trend --out results
```

## Verdict (part 1, 2026-11-13): PASS — but weak

Net of 0.10 % per unit traded:

| period | CAGR | Sharpe | max DD | alpha vs BTC (t) | beta |
|---|---|---|---|---|---|
| replication 2019-01 → 2025-03-14 | 8.4 % | 1.15 | −10.1 % | +3.7 % (1.57) | 0.06 |
| out of sample 2025-03-15 → 2026-10 | 0.9 % | 0.19 | −7.0 % | +0.8 % (0.21) | 0.07 |
| BTC buy-and-hold, out of sample | −4.7 % | 0.09 | −53 % | | |

How to read the PASS:

- **It replicates, partly.** Sharpe 1.15 on Binance vs "above 1.5" in the paper. The paper's
  2015–2018 years are not in our sample, and our sizing reading may differ (below).
- **Out of sample it is barely positive.** Sharpe 0.19 and alpha t = 0.21 over 19.5 months are
  indistinguishable from zero. The gate was "Sharpe > 0 and alpha > 0", so it passes, but this
  is "not dead", not "works". 2025 was −2.3 %, 2026 so far +1.8 %.
- **Exposure is low.** With fixed slots (1/180 of capital per coin × lookback) and a 25 % vol
  target on coins running at 80–100 % vol, gross exposure averages 0.07 (max 0.31). The paper's
  ~18 % CAGR implies much higher exposure, so it probably divides capital across *open*
  positions. Sharpe and alpha t-stats do not depend on this scale: levered ×3.5 to 25 % vol the
  replication is 29 % CAGR / −33 % max DD and out of sample 1.8 % / −23 %.
- Costs barely matter (turnover 4–5× a year, drag ≈ 0.4–0.5 % a year at 0.10 %).
- The BTC-only ensemble did better in replication (Sharpe 1.38) and worse out of sample (−0.07).

Next step per PREREG: forward paper-trade with these exact rules; HOLD-style re-check of the
out-of-sample statistics after six more months.
