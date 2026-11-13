# crypto-intraday

Kill test of two published intraday crypto rules on Binance USD-M perpetuals (BTCUSDT,
ETHUSDT, 5-minute klines, 2022-01 → 2026-10). Research only, no order entry.

- **R1**: long 22:00 → 00:00 UTC (Padyšák & Vojtko, SSRN 4081000; ~33 %/yr gross over 2015–21
  per Quantpedia).
- **R2**: first half-hour return (00:00–00:30 UTC) sets the direction of the last half-hour
  (Shen, Urquhart & Wang, Financial Review 2022).

Rules, costs and gates: [PREREG.md](PREREG.md). Output: [results/RESULTS.md](results/RESULTS.md).

```bash
cd crypto-intraday && uv sync
uv run python -m crypto_intraday.fetch      # ~45 MB, SHA-256 checked, to Data/binance-um-5m
uv run pytest && uv run crypto-intraday --out results
```

## Verdict (part 1, 2026-11-13): KILL

| rule | trades | gross bp | net @ 4 bp maker | net @ 10 bp taker (t) |
|---|---|---|---|---|
| R1 BTC (primary) | 1,764 | +1.4 (t 0.84) | −2.6 | −8.6 (−5.1) |
| R1 ETH | 1,764 | +1.5 | −2.5 | −8.5 (−3.7) |
| R2 BTC | 1,764 | −0.2 | −4.2 | −10.2 (−13.5) |
| R2 ETH | 1,764 | +0.0 | −4.0 | −10.0 (−10.3) |

- R1 is negative after taker fees in every year 2022–2026 and negative even at maker fees.
  Its gross edge (~1.4 bp/trade) is about a sixth of what 2015–21 implied (~9 bp).
- R2 has no gross edge at all; on the US-equity clock (09:30–10:00 ET → 15:30–16:00 ET) it is
  +0.6 bp gross on BTC and −1.2 on ETH.
- ETF benchmark window (15:00–16:00 ET) and the hour after: no significant drift (t ≤ 1.2).
- Hour-of-day table: 2–3 of 24 hours per period have |t| > 2, about what chance gives; the
  22:00 UTC hour is +3–4 bp gross but 23:00 gives back ~3 bp after the ETFs. Consistent with
  Schmidt & Kraft: time of day no longer predicts direction.
