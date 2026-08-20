# S7 PropGuard cluster Monte Carlo

Complete structures are grouped into overlapping trade clusters, and consecutive same-volatility-regime components remain one bootstrap block. Individual legs are never resampled. This preserves London/New York overlap, concurrent exposure and local regime clustering inside every sampled block.

## Identity, seed and limitations

| Field | Value |
|---|---|
| Symbol / timeframe | XAUUSD / M15 |
| Bars | 2000 |
| Bounds | 2026-07-21T05:45:00+00:00 to 2026-08-19T23:30:00+00:00 |
| Fingerprint | `85ab375472c64e92519d07f91ba0e1e06ec3c713e8921e88f81fef3d22bda900` |
| Seed | **20260820** |
| Simulations per mode / horizon | 2000 / 100 days |
| M1 coverage | partial: 93 / 2000 (4.65%) |
| Uniform fallback | `pessimistic_same_bar_no_subpath` |

The 2,000-bar M15 cache covers roughly 30 days of one symbol. It verifies deterministic cluster resampling and tail simulation; it cannot support a prop-survivability claim. That requires multiple years of clusters plus covering M1 and broker bid/ask, slippage, gap, swap, contract and margin observations across regimes. Partial M1 chronology is not mixed; the full run uses `pessimistic_same_bar_no_subpath`.

## Breach and target results

| Mode | Structures | Clusters | P daily 3% | Days to 3% | P daily 5% | Days to 5% | P total 6% | Days to 6% | P total 10% | Days to 10% | P target | Days to target | Min free margin p01 | Min free margin p50 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| hedge_pair | 48 | 12 | 0.00% | — | 0.00% | — | 0.00% | — | 0.00% | — | 0.00% | — | 96.84 | 98.71 |
| synthetic_breakout | 37 | 14 | 0.00% | — | 0.00% | — | 0.00% | — | 0.00% | — | 0.00% | — | 96.02 | 98.54 |
| contingent_hedge | 34 | 4 | 0.00% | — | 0.00% | — | 0.00% | — | 0.00% | — | 0.00% | — | 95.70 | 98.07 |
| oco_bracket | 40 | 15 | 0.00% | — | 0.00% | — | 0.00% | — | 0.00% | — | 0.00% | — | 97.62 | 99.03 |

Expected days are conditional on the breach or target occurring inside the 100-day horizon. Minimum free margin is normalized equity percent minus concurrent structures times `MAX_PAIR_RISK_PCT`; it is explicitly a risk-budget proxy, not broker margin.

## Gross/net path distributions and tail costs

| Mode | Gross pips p05/p50/p95 | Net pips p05/p50/p95 | Gross R p05/p50/p95 | Net R p05/p50/p95 | Mean spread cost | Mean slippage cost | Mean gap cost |
|---|---|---|---|---|---:|---:|---:|
| hedge_pair | -2205.49 / 2256.95 / 7557.89 | -3906.91 / 583.69 / 5905.96 | -10.8383 / 4.0540 / 21.1397 | -16.6111 / -1.4867 / 15.8309 | 1273.37 | 300.20 | 110.78 |
| synthetic_breakout | -7284.26 / 1361.80 / 11624.41 | -8015.80 / 673.64 / 10859.57 | -22.2053 / 2.2309 / 29.3432 | -24.5649 / -0.1584 / 26.9800 | 477.19 | 112.36 | 124.57 |
| contingent_hedge | -4817.20 / 3109.60 / 13297.90 | -7011.48 / 936.33 / 11045.82 | -18.9176 / 2.3498 / 27.6073 | -25.7821 / -3.9764 / 21.3673 | 969.77 | 227.85 | 972.55 |
| oco_bracket | -1783.69 / 4331.29 / 10112.74 | -2606.87 / 3522.41 / 9258.80 | -0.5675 / 19.0655 / 38.1936 | -3.2596 / 16.4081 / 35.6442 | 566.86 | 133.51 | 114.00 |

## Every empirical trade cluster

Every cluster and every complete structure ID is printed below; no leg-level sampling or winning-cluster filtering occurs.

| Mode | Cluster | Regime | Days | Structures | Sessions | Max concurrent | Structure IDs |
|---|---|---|---:|---:|---:|---:|---:|
| hedge_pair | 0 | high | 4 | 7 | london, new_york, tokyo | 3 | london:2026-07-21T08:00:00+00:00, new_york:2026-07-21T13:00:00+00:00, tokyo:2026-07-22T01:00:00+00:00, london:2026-07-22T08:00:00+00:00, new_york:2026-07-22T13:00:00+00:00, london:2026-07-23T08:00:00+00:00, tokyo:2026-07-24T01:00:00+00:00 |
| hedge_pair | 1 | low | 1 | 1 | new_york | 1 | new_york:2026-07-24T13:00:00+00:00 |
| hedge_pair | 2 | mid | 7 | 10 | london, new_york, tokyo | 3 | tokyo:2026-07-27T01:00:00+00:00, london:2026-07-27T08:00:00+00:00, new_york:2026-07-27T13:00:00+00:00, tokyo:2026-07-29T01:00:00+00:00, london:2026-07-29T08:00:00+00:00, new_york:2026-07-29T13:00:00+00:00, tokyo:2026-07-30T01:00:00+00:00, london:2026-07-30T08:00:00+00:00, tokyo:2026-07-31T01:00:00+00:00, new_york:2026-07-31T13:00:00+00:00 |
| hedge_pair | 3 | low | 3 | 5 | london, new_york, tokyo | 3 | tokyo:2026-08-03T01:00:00+00:00, london:2026-08-03T08:00:00+00:00, new_york:2026-08-03T13:00:00+00:00, london:2026-08-04T08:00:00+00:00, tokyo:2026-08-05T01:00:00+00:00 |
| hedge_pair | 4 | high | 2 | 4 | london, new_york, tokyo | 3 | london:2026-08-05T08:00:00+00:00, new_york:2026-08-05T13:00:00+00:00, tokyo:2026-08-06T01:00:00+00:00, london:2026-08-06T08:00:00+00:00 |
| hedge_pair | 5 | mid | 3 | 2 | london, new_york | 2 | london:2026-08-07T08:00:00+00:00, new_york:2026-08-07T13:00:00+00:00 |
| hedge_pair | 6 | low | 3 | 7 | london, new_york, tokyo | 3 | tokyo:2026-08-10T01:00:00+00:00, london:2026-08-10T08:00:00+00:00, new_york:2026-08-10T13:00:00+00:00, london:2026-08-11T08:00:00+00:00, new_york:2026-08-11T13:00:00+00:00, tokyo:2026-08-12T01:00:00+00:00, london:2026-08-12T08:00:00+00:00 |
| hedge_pair | 7 | high | 2 | 3 | london, new_york, tokyo | 3 | new_york:2026-08-12T13:00:00+00:00, tokyo:2026-08-13T01:00:00+00:00, london:2026-08-13T08:00:00+00:00 |
| hedge_pair | 8 | mid | 3 | 2 | london, new_york | 2 | london:2026-08-14T08:00:00+00:00, new_york:2026-08-14T13:00:00+00:00 |
| hedge_pair | 9 | high | 2 | 3 | london, new_york, tokyo | 3 | tokyo:2026-08-17T01:00:00+00:00, london:2026-08-17T08:00:00+00:00, new_york:2026-08-17T13:00:00+00:00 |
| hedge_pair | 10 | low | 1 | 2 | london, new_york | 2 | london:2026-08-18T08:00:00+00:00, new_york:2026-08-18T13:00:00+00:00 |
| hedge_pair | 11 | mid | 1 | 2 | london, tokyo | 2 | tokyo:2026-08-19T01:00:00+00:00, london:2026-08-19T08:00:00+00:00 |
| synthetic_breakout | 0 | low | 2 | 4 | london, new_york, tokyo | 2 | london:2026-07-21T08:00:00+00:00, new_york:2026-07-21T13:00:00+00:00, london:2026-07-22T08:00:00+00:00, tokyo:2026-07-22T01:00:00+00:00 |
| synthetic_breakout | 1 | high | 4 | 3 | london, new_york, tokyo | 3 | new_york:2026-07-22T13:00:00+00:00, london:2026-07-23T08:00:00+00:00, tokyo:2026-07-24T01:00:00+00:00 |
| synthetic_breakout | 2 | mid | 2 | 3 | london, new_york, tokyo | 3 | london:2026-07-27T08:00:00+00:00, new_york:2026-07-27T13:00:00+00:00, tokyo:2026-07-27T01:00:00+00:00 |
| synthetic_breakout | 3 | low | 1 | 1 | london | 1 | london:2026-07-29T08:00:00+00:00 |
| synthetic_breakout | 4 | mid | 5 | 5 | london, new_york, tokyo | 3 | new_york:2026-07-29T13:00:00+00:00, tokyo:2026-07-30T01:00:00+00:00, london:2026-07-30T08:00:00+00:00, tokyo:2026-07-31T01:00:00+00:00, new_york:2026-07-31T13:00:00+00:00 |
| synthetic_breakout | 5 | low | 1 | 2 | london | 1 | london:2026-08-03T08:00:00+00:00, london:2026-08-04T08:00:00+00:00 |
| synthetic_breakout | 6 | high | 3 | 5 | london, new_york, tokyo | 2 | new_york:2026-08-03T13:00:00+00:00, tokyo:2026-08-03T01:00:00+00:00, london:2026-08-05T08:00:00+00:00, new_york:2026-08-05T13:00:00+00:00, london:2026-08-06T08:00:00+00:00 |
| synthetic_breakout | 7 | mid | 3 | 2 | london, tokyo | 2 | london:2026-08-07T08:00:00+00:00, tokyo:2026-08-06T01:00:00+00:00 |
| synthetic_breakout | 8 | low | 1 | 1 | london | 1 | london:2026-08-10T08:00:00+00:00 |
| synthetic_breakout | 9 | mid | 1 | 1 | tokyo | 1 | tokyo:2026-08-10T01:00:00+00:00 |
| synthetic_breakout | 10 | low | 3 | 4 | london, tokyo | 2 | london:2026-08-11T08:00:00+00:00, tokyo:2026-08-12T01:00:00+00:00, london:2026-08-12T08:00:00+00:00, london:2026-08-13T08:00:00+00:00 |
| synthetic_breakout | 11 | mid | 5 | 3 | london, tokyo | 2 | tokyo:2026-08-13T01:00:00+00:00, london:2026-08-14T08:00:00+00:00, london:2026-08-17T08:00:00+00:00 |
| synthetic_breakout | 12 | low | 1 | 1 | london | 1 | london:2026-08-18T08:00:00+00:00 |
| synthetic_breakout | 13 | mid | 1 | 2 | london, tokyo | 2 | tokyo:2026-08-17T01:00:00+00:00, london:2026-08-19T08:00:00+00:00 |
| contingent_hedge | 0 | low | 6 | 6 | london, new_york, tokyo | 3 | london:2026-07-21T08:00:00+00:00, new_york:2026-07-21T13:00:00+00:00, tokyo:2026-07-22T01:00:00+00:00, new_york:2026-07-22T13:00:00+00:00, london:2026-07-23T08:00:00+00:00, tokyo:2026-07-24T01:00:00+00:00 |
| contingent_hedge | 1 | mid | 8 | 10 | london, new_york, tokyo | 4 | london:2026-07-27T08:00:00+00:00, new_york:2026-07-27T13:00:00+00:00, tokyo:2026-07-27T01:00:00+00:00, london:2026-07-29T08:00:00+00:00, new_york:2026-07-29T13:00:00+00:00, tokyo:2026-07-30T01:00:00+00:00, london:2026-07-30T08:00:00+00:00, tokyo:2026-07-31T01:00:00+00:00, new_york:2026-07-31T13:00:00+00:00, london:2026-08-03T08:00:00+00:00 |
| contingent_hedge | 2 | high | 2 | 5 | london, new_york, tokyo | 3 | tokyo:2026-08-03T01:00:00+00:00, new_york:2026-08-04T13:00:00+00:00, london:2026-08-05T08:00:00+00:00, new_york:2026-08-05T13:00:00+00:00, london:2026-08-06T08:00:00+00:00 |
| contingent_hedge | 3 | low | 13 | 13 | london, tokyo | 3 | london:2026-08-07T08:00:00+00:00, tokyo:2026-08-06T01:00:00+00:00, london:2026-08-10T08:00:00+00:00, tokyo:2026-08-10T01:00:00+00:00, london:2026-08-11T08:00:00+00:00, tokyo:2026-08-12T01:00:00+00:00, london:2026-08-12T08:00:00+00:00, london:2026-08-13T08:00:00+00:00, tokyo:2026-08-13T01:00:00+00:00, london:2026-08-14T08:00:00+00:00, london:2026-08-17T08:00:00+00:00, tokyo:2026-08-17T01:00:00+00:00, london:2026-08-19T08:00:00+00:00 |
| oco_bracket | 0 | low | 1 | 2 | london, new_york | 2 | london:2026-07-21T08:00:00+00:00, new_york:2026-07-21T13:00:00+00:00 |
| oco_bracket | 1 | high | 3 | 4 | london, new_york, tokyo | 2 | tokyo:2026-07-22T01:00:00+00:00, new_york:2026-07-22T13:00:00+00:00, london:2026-07-23T08:00:00+00:00, new_york:2026-07-23T13:00:00+00:00 |
| oco_bracket | 2 | mid | 2 | 3 | london, new_york, tokyo | 3 | new_york:2026-07-27T13:00:00+00:00, tokyo:2026-07-28T01:00:00+00:00, london:2026-07-28T08:00:00+00:00 |
| oco_bracket | 3 | low | 1 | 2 | london, new_york | 2 | london:2026-07-29T08:00:00+00:00, new_york:2026-07-29T13:00:00+00:00 |
| oco_bracket | 4 | high | 1 | 1 | tokyo | 1 | tokyo:2026-07-30T01:00:00+00:00 |
| oco_bracket | 5 | mid | 4 | 4 | london, new_york, tokyo | 2 | london:2026-07-30T08:00:00+00:00, new_york:2026-07-30T13:00:00+00:00, tokyo:2026-07-31T01:00:00+00:00, new_york:2026-07-31T13:00:00+00:00 |
| oco_bracket | 6 | high | 2 | 3 | london, new_york, tokyo | 2 | tokyo:2026-08-03T01:00:00+00:00, new_york:2026-08-03T13:00:00+00:00, london:2026-08-04T08:00:00+00:00 |
| oco_bracket | 7 | low | 1 | 1 | tokyo | 1 | tokyo:2026-08-05T01:00:00+00:00 |
| oco_bracket | 8 | mid | 1 | 1 | london | 1 | london:2026-08-05T08:00:00+00:00 |
| oco_bracket | 9 | low | 4 | 4 | london, new_york, tokyo | 2 | london:2026-08-06T08:00:00+00:00, new_york:2026-08-06T13:00:00+00:00, tokyo:2026-08-07T01:00:00+00:00, london:2026-08-07T08:00:00+00:00 |
| oco_bracket | 10 | mid | 1 | 1 | tokyo | 1 | tokyo:2026-08-10T01:00:00+00:00 |
| oco_bracket | 11 | low | 2 | 5 | london, new_york, tokyo | 3 | london:2026-08-10T08:00:00+00:00, new_york:2026-08-10T13:00:00+00:00, tokyo:2026-08-11T01:00:00+00:00, london:2026-08-11T08:00:00+00:00, new_york:2026-08-11T13:00:00+00:00 |
| oco_bracket | 12 | mid | 4 | 4 | london, new_york, tokyo | 2 | tokyo:2026-08-13T01:00:00+00:00, london:2026-08-13T08:00:00+00:00, new_york:2026-08-13T13:00:00+00:00, london:2026-08-14T08:00:00+00:00 |
| oco_bracket | 13 | low | 2 | 4 | london, new_york, tokyo | 3 | tokyo:2026-08-17T01:00:00+00:00, london:2026-08-17T08:00:00+00:00, new_york:2026-08-17T13:00:00+00:00, london:2026-08-18T08:00:00+00:00 |
| oco_bracket | 14 | mid | 1 | 1 | tokyo | 1 | tokyo:2026-08-19T01:00:00+00:00 |
