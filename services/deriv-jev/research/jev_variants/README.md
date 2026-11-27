# Former-Jev group: exit, stop and spread variants (2026-11-27)

Follow-up to the paper report of 27 November (items 2–4). It uses a tick-level replay of the paper trader's
former-Jev group (`bt_jev.py`) on 120 days of Deriv MT5 DSI10/20/30 ticks (`pull_dsi.py`, 1 Aug to 26 Nov 2026). The
first half is in-sample (1 Aug to 28 Sep) and the second half is out-of-sample (29 Sep to 26 Nov). Run on the
Contabo VPS with `/opt/dsi-analogs/.venv` (numba); data is in `/opt/dsi-analogs/jev`.

The replay matches the paper trader: 43 of its 45 jev trades are reproduced (same side, entry within 10 s), with a
median R difference of −0.002 and a mean absolute difference of 0.016R. The other trades fall in the paper trader's
downtime on 26–27 Nov.

## Results (R per trade, pooled over the 5 strategies)

| | In-sample | Out-of-sample |
|---|---|---|
| Current rules (trail 1.5R, 8 h limit) | −0.031 (1,421 trades, t −0.5) | +0.008 (1,486, t 0.1) |
| **Item 2:** trail 3R, 24 h limit (best in-sample of 16 trail × limit variants) | +0.095 (905, t 0.8) | +0.057 (1,002, t 0.5) |
| Item 2: trail 3R, 8 h | +0.004 | +0.089 (t 1.0) |
| Item 2: longer limit only (trail 1.5R, 16 h to 7 days) | −0.042 | +0.009 |
| **Item 3:** skip signals that need a widened stop (current exits) | +0.022 (149, t 0.2) | −0.074 (169, t −0.6) |
| **Item 4:** skip entry spread < 1.0 × prior-hour median (current exits) | −0.014 (1,288) | −0.012 (1,351) |

- The two days of paper results (+0.42R per trade) were one trending day. Over four months the current rules make
  about 0R per trade.
- **Item 2:** a wider trail helps in both halves; a longer time limit on its own does nothing, because only about
  2% of trades reach 8 hours. No variant is significant (best out-of-sample t ≈ 1).
- **Item 3:** the broker minimum widens 100% of the M1/M5 signals and 5% of DSI30 M15. Keeping only natural stops
  leaves about one trade a day, and it loses out of sample.
- **Item 4:** by entry-spread quintile (`spread_buckets.py`), the narrowest fifth averages +0.059R (± 0.07). That is
  no worse than the others, so a narrow spread does not flag losing entries. The filter does not help.

All three are now running as extra paper accounts (`../../paper_variants.json`), so the live feed can confirm or
reject them alongside the current rules.
