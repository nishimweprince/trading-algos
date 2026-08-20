# Phase 3 exploratory development

A negative result closes the tested family and is publishable. A positive result is
exploratory evidence only. No §9 gate is claimed passed. No coordinate is promoted.
Live trading is not enabled. `P3H-20260820` was not accessed.

| Field | Value |
|---|---|
| Protocol commit | `27a85ef` |
| Coordinates | 104 |
| Candidate-list SHA-256 | `eb2c04f5edd92e86a8e87ec7d903c6f342655e94949dc4d4ee9298097bf47146` |
| Development raw SHA-256 | `c45d540d1d06c00459e41d7c29fc1d8844fe599c16e03bc348ac0138eaf63fa1` |
| Development canonical SHA-256 | `77240977e64c2fb44ef18f1dc64bd4967d8a5a1370d49f79b4000b3bdd96516f` |
| Bars | 2026-03-19T07:45:00+00:00 … 2026-08-20T10:45:00+00:00 |
| Evaluations | 952 / 954 (two holdout slots unused) |
| Holdout | locked (accessed=False) |
| Unseen gross pips / R | 4059.7300 / 17.2075 |
| Unseen net pips / R | 3632.2300 / 15.4167 |
| Unseen stress gross pips / R | 4059.7300 / 17.2075 |
| Unseen stress net pips / R | 3204.7300 / 13.6259 |
| Unseen completed structures | 83 |
| DSR | computed; Sharpe 0.0921 vs expected-max 2.5444; probability 0.0 (n=83, trials=104) |
| PBO | not_computable (CSCV of all 104 coordinates on disjoint unseen blocks would exceed the cap) |
| Full-development selected | `smoothed_stop:orb_atr14_blend:sl2:oco_bracket` |
| Neighbourhood plateau | 1 within 0.05 R |

## Unseen folds

| Fold | Selected | Test net R | Stress net R |
|---|---|---|---|
| 0 | `smoothed_stop:orb_atr14_blend:sl2:oco_bracket` | 2.3318 | 2.1849 |
| 1 | `smoothed_stop:orb_atr14_blend:sl2:oco_bracket` | 5.9282 | 5.6848 |
| 2 | `smoothed_stop:orb_atr14_blend:sl2:oco_bracket` | -2.2728 | -2.5605 |
| 3 | `smoothed_stop:orb_atr14_blend:sl2:oco_bracket` | -0.8368 | -1.0689 |
| 4 | `smoothed_stop:orb_atr14_blend:sl2:oco_bracket` | 4.2061 | 4.0555 |
| 5 | `smoothed_stop:orb_atr14_blend:sl2:oco_bracket` | 2.4818 | 2.1874 |
| 6 | `smoothed_stop:orb_atr14_blend:sl2:oco_bracket` | 2.9898 | 2.7674 |
| 7 | `smoothed_stop:orb_atr14_blend:sl2:oco_bracket` | 0.5885 | 0.3754 |

## Unseen session attribution (net)

| Session | Structures | Net pips | Net R |
|---|---:|---:|---:|
| tokyo | 28 | 965.7240 | 4.6818 |
| london | 29 | 1891.6081 | 7.6105 |
| new_york | 26 | -400.2020 | -0.2824 |

Every training evaluation and losing coordinate is retained in the JSON companion.
Commission and swap remain labelled missing. Costs are modeled, not broker-measured.
This selected coordinate is the only candidate the prospective holdout may later see;
it is not a production configuration.
