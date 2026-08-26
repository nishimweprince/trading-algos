# S9 regime and trend attribution

A symmetric straddle in a trending instrument collects the drift. That is a real effect and a regime-dependent one, so any configuration whose edge comes predominantly from one direction or one regime is flagged here rather than presented as a strategy result.

Over this window gold rose from 4066.03 to 4517.73, a move of +4517.0 pips. Trend regime is the trailing 5-day slope of the daily close, labelled `up` or `down` beyond ±50 pips per day and `flat` inside that deadband; the first 5 days are `warmup` and are reported, not dropped.

## Run identity

| Field | Value |
|---|---|
| Symbol | XAUUSD |
| Timeframe | M15 |
| Source | local |
| Bars | 2000 |
| First bar (UTC) | 2026-07-21T05:45:00+00:00 |
| Last bar (UTC) | 2026-08-19T23:30:00+00:00 |
| Candle fingerprint (sha256) | `85ab375472c64e92519d07f91ba0e1e06ec3c713e8921e88f81fef3d22bda900` |
| Entry modes | hedge_pair, synthetic_breakout, contingent_hedge, oco_bracket |
| Calendar split (UTC) | 2026-08-05T02:37:30+00:00 |
| Trend day counts | down 4, flat 4, up 13, warmup 5 |
| Concentration threshold | 70% |

## M1 coverage and subpath fallback

| Field | Value |
|---|---|
| `INTRABAR_MODE` | m1_conservative |
| M1 coverage status | **partial** |
| M1 bars loaded | 2000 |
| Parent bars with covering M1 | 93 / 2000 (4.65%) |
| M1 subpath chronology used | no |
| Fallback | pessimistic_same_bar_no_subpath |

M1 bars were present but covered only 93 of 2000 parent bars (4.65% of the window). Mixing M1 chronology on part of the window with the fallback on the rest would make results inside one study incomparable, so no M1 chronology was used: the whole window was resolved with the conservative pessimistic_same_bar_no_subpath fallback, in which a bar touching both the stop and the target is taken as the stop.

## Directional and regime flags

A flag fires when at least 70% of the surviving winners fall on one side, or when one calendar half or one trend regime carries that share of the absolute net R. A flag is not a verdict; it marks a result that cannot be read as direction-neutral.

| Mode | Reason | Detail |
|---|---|---|
| hedge_pair | directional_winner_concentration | 6 of 8 surviving winners were long (75.0%); the interval is [40.9%, 92.9%] |
| synthetic_breakout | directional_winner_concentration | 6 of 7 surviving winners were long (85.7%); the interval is [48.7%, 97.4%] |
| contingent_hedge | directional_winner_concentration | 4 of 4 surviving winners were long (100.0%); the interval is [51.0%, 100.0%] |
| oco_bracket | calendar_half_concentration | the second half carries 73.9% of the absolute net R (+4.2746R of +5.7840R overall) |

## Every mode and split

| Mode | Split | Key | Completed | Gross pips | Net pips | Gross R | Net R | Gross exp R | Net exp R | Gross PF | Net PF | Win excl BE | TP | Long winners | Short winners | Long winner share | CI low | CI high | Net R from long | Net R from short | Long share of |net R| |
|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| hedge_pair | all | all | 48 | 727.70 | 727.70 | 1.2911 | 1.2911 | 0.0269 | 0.0269 | 1.1428 | 1.1428 | 40.00% | 8 | 6 | 2 | 75.00% | 40.93% | 92.85% | 6.4156 | -3.1245 | 67.25% |
| hedge_pair | calendar_half | first | 23 | -330.50 | -330.50 | -1.0576 | -1.0576 | -0.0460 | -0.0460 | 0.8517 | 0.8517 | 35.00% | 3 | 2 | 1 | 66.67% | 20.77% | 93.85% | 0.8196 | -1.8771 | 30.39% |
| hedge_pair | calendar_half | second | 25 | 1058.20 | 1058.20 | 2.3486 | 2.3486 | 0.0939 | 0.0939 | 1.3692 | 1.3692 | 45.00% | 5 | 4 | 1 | 80.00% | 37.55% | 96.38% | 5.5960 | -1.2474 | 81.77% |
| hedge_pair | trend_regime | up | 30 | 598.10 | 598.10 | 2.8038 | 2.8038 | 0.0935 | 0.0935 | 1.1736 | 1.1736 | 43.48% | 6 | 5 | 1 | 83.33% | 43.65% | 96.99% | 7.4538 | -2.6500 | 73.77% |
| hedge_pair | trend_regime | down | 4 | 128.60 | 128.60 | 0.6493 | 0.6493 | 0.1623 | 0.1623 | 1.4261 | 1.4261 | 25.00% | 1 | 0 | 1 | 0.00% | 0.00% | 79.35% | -0.0364 | 0.6857 | 5.04% |
| hedge_pair | trend_regime | flat | 6 | 265.70 | 265.70 | -0.4577 | -0.4577 | -0.0763 | -0.0763 | 1.8236 | 1.8236 | 60.00% | 0 | 0 | 0 | — | — | — | -0.9188 | 0.4611 | 66.58% |
| hedge_pair | trend_regime | warmup | 8 | -264.70 | -264.70 | -1.7043 | -1.7043 | -0.2130 | -0.2130 | 0.7418 | 0.7418 | 25.00% | 1 | 1 | 0 | 100.00% | 20.65% | 100.00% | -0.0830 | -1.6213 | 4.87% |
| hedge_pair | session | london | 19 | -840.40 | -840.40 | -4.3803 | -4.3803 | -0.2305 | -0.2305 | 0.6960 | 0.6960 | 31.58% | 3 | 2 | 1 | 66.67% | 20.77% | 93.85% | 0.8689 | -3.2492 | 21.10% |
| hedge_pair | session | new_york | 15 | 1027.00 | 1027.00 | 4.1611 | 4.1611 | 0.2774 | 0.2774 | 2.1053 | 2.1053 | 54.55% | 3 | 2 | 1 | 66.67% | 20.77% | 93.85% | 2.1587 | 2.0024 | 51.88% |
| hedge_pair | session | tokyo | 14 | 541.10 | 541.10 | 1.5103 | 1.5103 | 0.1079 | 0.1079 | 1.3861 | 1.3861 | 40.00% | 2 | 2 | 0 | 100.00% | 34.24% | 100.00% | 3.3879 | -1.8776 | 64.34% |
| synthetic_breakout | all | all | 37 | 551.60 | 551.60 | 0.8969 | 0.8969 | 0.0242 | 0.0242 | 1.0898 | 1.0898 | 40.54% | 7 | 6 | 1 | 85.71% | 48.69% | 97.43% | 14.0524 | 5.0651 | 73.51% |
| synthetic_breakout | calendar_half | first | 20 | 1138.60 | 1138.60 | 2.6937 | 2.6937 | 0.1347 | 0.1347 | 1.3727 | 1.3727 | 45.00% | 4 | 3 | 1 | 75.00% | 30.06% | 95.44% | 6.7441 | 5.0651 | 57.11% |
| synthetic_breakout | calendar_half | second | 17 | -587.00 | -587.00 | -1.7968 | -1.7968 | -0.1057 | -0.1057 | 0.8099 | 0.8099 | 35.29% | 3 | 3 | 0 | 100.00% | 43.85% | 100.00% | 7.3084 | 0.0000 | 100.00% |
| synthetic_breakout | trend_regime | up | 17 | 944.90 | 944.90 | 2.8949 | 2.8949 | 0.1703 | 0.1703 | 1.4339 | 1.4339 | 47.06% | 4 | 3 | 1 | 75.00% | 30.06% | 95.44% | 8.0524 | 2.0000 | 80.10% |
| synthetic_breakout | trend_regime | down | 7 | -172.70 | -172.70 | -0.3639 | -0.3639 | -0.0520 | -0.0520 | 0.8600 | 0.8600 | 42.86% | 0 | 0 | 0 | — | — | — | 0.0000 | 2.8556 | 0.00% |
| synthetic_breakout | trend_regime | flat | 6 | 787.50 | 787.50 | 0.8219 | 0.8219 | 0.1370 | 0.1370 | 1.7618 | 1.7618 | 33.33% | 2 | 2 | 0 | 100.00% | 34.24% | 100.00% | 4.0000 | 0.0000 | 100.00% |
| synthetic_breakout | trend_regime | warmup | 7 | -1008.10 | -1008.10 | -2.4560 | -2.4560 | -0.3509 | -0.3509 | 0.4059 | 0.4059 | 28.57% | 1 | 1 | 0 | 100.00% | 20.65% | 100.00% | 2.0000 | 0.2096 | 90.52% |
| synthetic_breakout | session | london | 19 | -772.80 | -772.80 | -3.3020 | -3.3020 | -0.1738 | -0.1738 | 0.7432 | 0.7432 | 31.58% | 3 | 2 | 1 | 66.67% | 20.77% | 93.85% | 4.8304 | 3.1851 | 60.26% |
| synthetic_breakout | session | new_york | 7 | 2206.70 | 2206.70 | 5.5407 | 5.5407 | 0.7915 | 0.7915 | 7.2407 | 7.2407 | 85.71% | 2 | 2 | 0 | 100.00% | 34.24% | 100.00% | 5.2220 | 1.2652 | 80.50% |
| synthetic_breakout | session | tokyo | 11 | -882.30 | -882.30 | -1.3418 | -1.3418 | -0.1220 | -0.1220 | 0.6825 | 0.6825 | 27.27% | 2 | 2 | 0 | 100.00% | 34.24% | 100.00% | 4.0000 | 0.6148 | 86.68% |
| contingent_hedge | all | all | 34 | 1012.10 | 1012.10 | 0.7336 | 0.7336 | 0.0216 | 0.0216 | 1.1695 | 1.1695 | 55.88% | 4 | 4 | 0 | 100.00% | 51.01% | 100.00% | 11.9567 | 3.4956 | 77.38% |
| contingent_hedge | calendar_half | first | 18 | 1768.20 | 1768.20 | 5.4326 | 5.4326 | 0.3018 | 0.3018 | 1.9562 | 1.9562 | 66.67% | 3 | 3 | 0 | 100.00% | 43.85% | 100.00% | 7.4141 | 1.9259 | 79.38% |
| contingent_hedge | calendar_half | second | 16 | -756.10 | -756.10 | -4.6990 | -4.6990 | -0.2937 | -0.2937 | 0.8165 | 0.8165 | 43.75% | 1 | 1 | 0 | 100.00% | 20.65% | 100.00% | 4.5426 | 1.5697 | 74.32% |
| contingent_hedge | trend_regime | up | 18 | -913.70 | -913.70 | -3.8833 | -3.8833 | -0.2157 | -0.2157 | 0.7818 | 0.7818 | 50.00% | 2 | 2 | 0 | 100.00% | 34.24% | 100.00% | 5.7054 | 1.2576 | 81.94% |
| contingent_hedge | trend_regime | down | 6 | 1424.30 | 1424.30 | 3.0630 | 3.0630 | 0.5105 | 0.5105 | 9.6636 | 9.6636 | 83.33% | 0 | 0 | 0 | — | — | — | 1.2745 | 2.2885 | 35.77% |
| contingent_hedge | trend_regime | flat | 4 | -295.40 | -295.40 | -1.1557 | -1.1557 | -0.2889 | -0.2889 | 0.7027 | 0.7027 | 25.00% | 1 | 1 | 0 | 100.00% | 20.65% | 100.00% | 1.5000 | -0.7369 | 67.06% |
| contingent_hedge | trend_regime | warmup | 6 | 796.90 | 796.90 | 2.7096 | 2.7096 | 0.4516 | 0.4516 | 2.2779 | 2.2779 | 66.67% | 1 | 1 | 0 | 100.00% | 20.65% | 100.00% | 3.4767 | 0.6864 | 83.51% |
| contingent_hedge | session | london | 16 | -436.80 | -436.80 | -2.0950 | -2.0950 | -0.1309 | -0.1309 | 0.8534 | 0.8534 | 50.00% | 2 | 2 | 0 | 100.00% | 34.24% | 100.00% | 5.0099 | 3.2221 | 60.86% |
| contingent_hedge | session | new_york | 7 | 1637.60 | 1637.60 | 3.8721 | 3.8721 | 0.5532 | 0.5532 | 6.9484 | 6.9484 | 85.71% | 1 | 1 | 0 | 100.00% | 20.65% | 100.00% | 4.1723 | -0.3002 | 93.29% |
| contingent_hedge | session | tokyo | 11 | -188.70 | -188.70 | -1.0435 | -1.0435 | -0.0949 | -0.0949 | 0.9305 | 0.9305 | 45.45% | 1 | 1 | 0 | 100.00% | 20.65% | 100.00% | 2.7745 | 0.5737 | 82.87% |
| oco_bracket | all | all | 40 | 1353.99 | 1353.99 | 5.7840 | 5.7840 | 0.1446 | 0.1446 | 1.2181 | 1.2181 | 47.50% | 4 | 2 | 2 | 50.00% | 15.00% | 85.00% | 13.3436 | 11.4804 | 53.75% |
| oco_bracket | calendar_half | first | 20 | -118.55 | -118.55 | 1.5094 | 1.5094 | 0.0755 | 0.0755 | 0.9632 | 0.9632 | 50.00% | 2 | 1 | 1 | 50.00% | 9.45% | 90.55% | 3.8818 | 7.1614 | 35.15% |
| oco_bracket | calendar_half | second | 20 | 1472.54 | 1472.54 | 4.2746 | 4.2746 | 0.2137 | 0.2137 | 1.4936 | 1.4936 | 45.00% | 2 | 1 | 1 | 50.00% | 9.45% | 90.55% | 9.4618 | 4.3190 | 68.66% |
| oco_bracket | trend_regime | up | 24 | 265.59 | 265.59 | 4.3012 | 4.3012 | 0.1792 | 0.1792 | 1.0644 | 1.0644 | 45.83% | 3 | 2 | 1 | 66.67% | 20.77% | 93.85% | 12.7576 | 3.2238 | 79.83% |
| oco_bracket | trend_regime | down | 5 | 551.40 | 551.40 | 2.6895 | 2.6895 | 0.5379 | 0.5379 | 2.1095 | 2.1095 | 60.00% | 1 | 0 | 1 | 0.00% | 0.00% | 79.35% | 0.0000 | 4.6895 | 0.00% |
| oco_bracket | trend_regime | flat | 5 | 908.22 | 908.22 | 0.4811 | 0.4811 | 0.0962 | 0.0962 | 2.7700 | 2.7700 | 40.00% | 0 | 0 | 0 | — | — | — | 0.0000 | 2.8409 | 0.00% |
| oco_bracket | trend_regime | warmup | 6 | -371.22 | -371.22 | -1.6878 | -1.6878 | -0.2813 | -0.2813 | 0.6538 | 0.6538 | 50.00% | 0 | 0 | 0 | — | — | — | 0.5860 | 0.7262 | 44.66% |
| oco_bracket | session | london | 15 | 530.24 | 530.24 | 2.9814 | 2.9814 | 0.1988 | 0.1988 | 1.2837 | 1.2837 | 46.67% | 2 | 0 | 2 | 0.00% | 0.00% | 65.76% | 4.4665 | 6.5149 | 40.67% |
| oco_bracket | session | new_york | 13 | -1879.18 | -1879.18 | -3.2233 | -3.2233 | -0.2479 | -0.2479 | 0.4248 | 0.4248 | 23.08% | 1 | 1 | 0 | 100.00% | 20.65% | 100.00% | 3.0000 | 1.8166 | 62.28% |
| oco_bracket | session | tokyo | 12 | 2702.93 | 2702.93 | 6.0260 | 6.0260 | 0.5022 | 0.5022 | 3.5228 | 3.5228 | 75.00% | 1 | 1 | 0 | 100.00% | 20.65% | 100.00% | 5.8771 | 3.1488 | 65.11% |

## Caveats

- 2000 M15 bars covering 26 calendar days, of which 4 were labelled `down`. A window with almost no down-trend cannot establish that a strategy works in one, and the `down` rows here hold a handful of structures each.
- Surviving-winner counts are small. The long-share intervals are wide enough to include an even split in most rows; the flag marks concentration, it does not establish it.
- M1 bars were present but covered only 93 of 2000 parent bars (4.65% of the window). Mixing M1 chronology on part of the window with the fallback on the rest would make results inside one study incomparable, so no M1 chronology was used: the whole window was resolved with the conservative pessimistic_same_bar_no_subpath fallback, in which a bar touching both the stop and the target is taken as the stop.
- `Net R from long` and `Net R from short` attribute a structure to the side of its single winning leg. Structures with no winning leg, or with two, are counted in the totals but in neither directional column, so the two columns need not sum to net R.
- Calendar halves split the window at its midpoint by time, not by structure count.
