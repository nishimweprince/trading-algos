Work plan P1.4: logged vs re-costed P&L for the baseline week, plus the live strategy week for comparison. Regenerate with:

```bash
npm run research:recost -- --csv reports/baseline-2026-09-25/trades-live-7d.csv \
  --live reports/strategy-week/trades-7d-2026-09-18T11-10-01-680Z.csv --seed 1 \
  --title "Re-cost — 2026-09-25" --preamble reports/recost-2026-09-25/preamble.md \
  --postscript reports/recost-2026-09-25/postscript.md --out reports/recost-2026-09-25.md
```
