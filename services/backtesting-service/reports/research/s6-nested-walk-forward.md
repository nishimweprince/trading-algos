# S6 nested walk-forward

The protocol was frozen in the specification before final-holdout access. The §9 scorecard blocked redesign, so the only candidates are the four incumbent entry modes; all other named model parameters remain explicit singleton axes inside every coordinate.

## Run identity and limits

| Field | Value |
|---|---|
| Symbol / timeframe | XAUUSD / M15 |
| Bars | 2000 |
| Bounds | 2026-07-21T05:45:00+00:00 to 2026-08-19T23:30:00+00:00 |
| Fingerprint | `85ab375472c64e92519d07f91ba0e1e06ec3c713e8921e88f81fef3d22bda900` |
| Candidates | 4 |
| Rolling folds | 4 |
| Train / test / holdout bars | 800 / 200 / 400 |
| M1 coverage | partial: 93 / 2000 (4.65%) |
| Uniform fallback | `pessimistic_same_bar_no_subpath` |

The 2,000-bar M15 cache covers roughly 30 days of one symbol. It verifies the harness; it cannot select a strategy or establish an edge. Multiple years of contiguous M15 with covering M1 and measured broker costs across varied regimes are required. Partial M1 chronology is not mixed; the full run uses `pessimistic_same_bar_no_subpath`.

## Candidate coordinates

| # | Mode | Anchors | ORB | Delay | MaxAge | SL | RR | Lock | Hedge ratios |
|---|---|---|---:|---:|---:|---:|---:|---:|---:|
| 0 | hedge_pair | tokyo:Asia/Tokyo:09:00, london:Europe/London:08:00, new_york:America/New_York:08:00 | 60 | 15 | 24 | 2 | 3 | absolute:20 | 0/1 |
| 1 | synthetic_breakout | tokyo:Asia/Tokyo:09:00, london:Europe/London:08:00, new_york:America/New_York:08:00 | 60 | 15 | 24 | 2 | 3 | absolute:20 | 0/1 |
| 2 | contingent_hedge | tokyo:Asia/Tokyo:09:00, london:Europe/London:08:00, new_york:America/New_York:08:00 | 60 | 15 | 24 | 2 | 3 | absolute:20 | 0/1 |
| 3 | oco_bracket | tokyo:Asia/Tokyo:09:00, london:Europe/London:08:00, new_york:America/New_York:08:00 | 60 | 15 | 24 | 2 | 3 | absolute:20 | 0/1 |

## Per-fold unseen results

| Fold | Selected mode | Train net exp R | Unseen gross pips | Unseen net pips | Unseen gross R | Unseen net R | Unseen net exp R |
|---|---|---:|---:|---:|---:|---:|---:|
| 0 | contingent_hedge | 0.1733 | -281.90 | -281.90 | -0.5084 | -0.5084 | 0.2430 |
| 1 | oco_bracket | 0.0860 | -739.84 | -739.84 | -2.3819 | -2.3819 | -1.0000 |
| 2 | contingent_hedge | 0.1505 | 372.50 | 372.50 | 0.7330 | 0.7330 | 0.2735 |
| 3 | synthetic_breakout | 0.1077 | -1067.60 | -1067.60 | -3.7137 | -3.7137 | -0.9284 |

## Unseen-only aggregate and final holdout

| Result | Completed | Gross pips | Net pips | Gross R | Net R | Gross exp R | Net exp R |
|---|---:|---:|---:|---:|---:|---:|---:|
| Four rolling tests | 11 | -1716.84 | -1716.84 | -5.8710 | -5.8710 | -0.5137 | -0.5137 |
| Final untouched holdout | 8 | 236.09 | 236.09 | 1.2354 | 1.2354 | 0.1019 | 0.1019 |

The rolling aggregate lists only evaluation IDs whose role is `unseen_test`; no training or CSCV block result is included. The final holdout is reported separately.

## Deflated Sharpe and CSCV probability of backtest overfitting

| Statistic | Value |
|---|---:|
| DSR status | computed |
| Unseen structure observations | 11 |
| Raw Sharpe | -0.517033 |
| Expected max Sharpe (4 trials) | 1.052123 |
| Deflated Sharpe probability | 0.0305% |
| CSCV blocks / splits | 8 / 70 |
| Probability of backtest overfitting | 40.0000% |

DSR formula: `DSR=Phi((SR-SR*)/sqrt((1-skew*SR+((kurtosis-1)/4)*SR^2)/(n-1))); SR* uses the expected maximum of N independent normal trials`.

CSCV formula: `PBO = fraction of CSCV splits where the in-sample winner ranks in the bottom half out of sample (logit <= 0)`. Every CSCV split and every configuration evaluation is present in the JSON artifact; no losing cell or fold is omitted.

## Every evaluation

| Evaluation | Phase | Role | Mode | Bars | Completed | Gross pips | Net pips | Gross R | Net R | Gross exp R | Net exp R |
|---|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| fold-0-train-config-0 | rolling_fold | training | hedge_pair | 800 | 18 | -519.10 | -519.10 | -1.3071 | -1.3071 | -0.0726 | -0.0726 |
| fold-0-train-config-1 | rolling_fold | training | synthetic_breakout | 800 | 15 | -113.70 | -113.70 | 1.0713 | 1.0713 | 0.0927 | 0.0927 |
| fold-0-train-config-2 | rolling_fold | training | contingent_hedge | 800 | 14 | 229.30 | 229.30 | 2.1071 | 2.1071 | 0.1733 | 0.1733 |
| fold-0-train-config-3 | rolling_fold | training | oco_bracket | 800 | 16 | -614.82 | -614.82 | -0.0971 | -0.0971 | -0.0061 | -0.0061 |
| fold-0-unseen-test | rolling_fold | unseen_test | contingent_hedge | 200 | 1 | -281.90 | -281.90 | -0.5084 | -0.5084 | 0.2430 | 0.2430 |
| fold-1-train-config-0 | rolling_fold | training | hedge_pair | 800 | 17 | -851.60 | -851.60 | -2.5570 | -2.5570 | -0.1397 | -0.1397 |
| fold-1-train-config-1 | rolling_fold | training | synthetic_breakout | 800 | 14 | -2229.60 | -2229.60 | -6.5913 | -6.5913 | -0.4886 | -0.4886 |
| fold-1-train-config-2 | rolling_fold | training | contingent_hedge | 800 | 12 | -1733.70 | -1733.70 | -4.6608 | -4.6608 | -0.3465 | -0.3465 |
| fold-1-train-config-3 | rolling_fold | training | oco_bracket | 800 | 15 | 86.29 | 86.29 | 2.0074 | 2.0074 | 0.0860 | 0.0860 |
| fold-1-unseen-test | rolling_fold | unseen_test | oco_bracket | 200 | 3 | -739.84 | -739.84 | -2.3819 | -2.3819 | -1.0000 | -1.0000 |
| fold-2-train-config-0 | rolling_fold | training | hedge_pair | 800 | 18 | -155.90 | -155.90 | 0.1134 | 0.1134 | 0.0063 | 0.0063 |
| fold-2-train-config-1 | rolling_fold | training | synthetic_breakout | 800 | 16 | 638.30 | 638.30 | 0.1555 | 0.1555 | 0.0097 | 0.0097 |
| fold-2-train-config-2 | rolling_fold | training | contingent_hedge | 800 | 14 | 1230.50 | 1230.50 | 2.1069 | 2.1069 | 0.1505 | 0.1505 |
| fold-2-train-config-3 | rolling_fold | training | oco_bracket | 800 | 16 | -440.77 | -440.77 | 0.4594 | 0.4594 | -0.0099 | -0.0099 |
| fold-2-unseen-test | rolling_fold | unseen_test | contingent_hedge | 200 | 3 | 372.50 | 372.50 | 0.7330 | 0.7330 | 0.2735 | 0.2735 |
| fold-3-train-config-0 | rolling_fold | training | hedge_pair | 800 | 19 | 575.20 | 575.20 | 1.8978 | 1.8978 | 0.1045 | 0.1045 |
| fold-3-train-config-1 | rolling_fold | training | synthetic_breakout | 800 | 16 | 1266.90 | 1266.90 | 1.6362 | 1.6362 | 0.1077 | 0.1077 |
| fold-3-train-config-2 | rolling_fold | training | contingent_hedge | 800 | 15 | -58.50 | -58.50 | -0.7038 | -0.7038 | -0.0411 | -0.0411 |
| fold-3-train-config-3 | rolling_fold | training | oco_bracket | 800 | 17 | 291.44 | 291.44 | 1.4940 | 1.4940 | 0.0416 | 0.0416 |
| fold-3-unseen-test | rolling_fold | unseen_test | synthetic_breakout | 200 | 4 | -1067.60 | -1067.60 | -3.7137 | -3.7137 | -0.9284 | -0.9284 |
| final-selection-train-config-0 | final_selection | pre_holdout_training | hedge_pair | 1600 | 38 | -424.40 | -424.40 | -2.3820 | -2.3820 | -0.0627 | -0.0627 |
| final-selection-train-config-1 | final_selection | pre_holdout_training | synthetic_breakout | 1600 | 31 | 1364.00 | 1364.00 | 2.1312 | 2.1312 | 0.0687 | 0.0687 |
| final-selection-train-config-2 | final_selection | pre_holdout_training | contingent_hedge | 1600 | 29 | -296.20 | -296.20 | -2.6607 | -2.6607 | -0.0917 | -0.0917 |
| final-selection-train-config-3 | final_selection | pre_holdout_training | oco_bracket | 1600 | 32 | 778.08 | 778.08 | 3.5987 | 3.5987 | 0.0865 | 0.0865 |
| final-holdout-unseen | final_holdout | final_unseen_holdout | oco_bracket | 400 | 8 | 236.09 | 236.09 | 1.2354 | 1.2354 | 0.1019 | 0.1019 |
| cscv-block-0-config-0 | cscv | pre_holdout_cscv_block | hedge_pair | 200 | 4 | -44.30 | -44.30 | -0.1202 | -0.1202 | -0.0301 | -0.0301 |
| cscv-block-0-config-1 | cscv | pre_holdout_cscv_block | synthetic_breakout | 200 | 4 | -392.60 | -392.60 | -0.8066 | -0.8066 | -0.2017 | -0.2017 |
| cscv-block-0-config-2 | cscv | pre_holdout_cscv_block | contingent_hedge | 200 | 2 | 872.10 | 872.10 | 3.6940 | 3.6940 | 2.0406 | 2.0406 |
| cscv-block-0-config-3 | cscv | pre_holdout_cscv_block | oco_bracket | 200 | 3 | -734.09 | -734.09 | -2.3323 | -2.3323 | -0.4713 | -0.4713 |
| cscv-block-1-config-0 | cscv | pre_holdout_cscv_block | hedge_pair | 200 | 3 | -214.90 | -214.90 | -1.0214 | -1.0214 | -0.3405 | -0.3405 |
| cscv-block-1-config-1 | cscv | pre_holdout_cscv_block | synthetic_breakout | 200 | 1 | -688.50 | -688.50 | -2.0576 | -2.0576 | -0.8934 | -0.8934 |
| cscv-block-1-config-2 | cscv | pre_holdout_cscv_block | contingent_hedge | 200 | 1 | -687.10 | -687.10 | -2.4033 | -2.4033 | -1.4536 | -1.4536 |
| cscv-block-1-config-3 | cscv | pre_holdout_cscv_block | oco_bracket | 200 | 2 | 470.70 | 470.70 | 1.2185 | 1.2185 | 0.5467 | 0.5467 |
| cscv-block-2-config-0 | cscv | pre_holdout_cscv_block | hedge_pair | 200 | 4 | 280.90 | 280.90 | 1.5273 | 1.5273 | 0.3818 | 0.3818 |
| cscv-block-2-config-1 | cscv | pre_holdout_cscv_block | synthetic_breakout | 200 | 4 | -200.20 | -200.20 | -0.0036 | -0.0036 | -0.0009 | -0.0009 |
| cscv-block-2-config-2 | cscv | pre_holdout_cscv_block | contingent_hedge | 200 | 3 | -274.50 | -274.50 | -0.9314 | -0.9314 | -0.2705 | -0.2705 |
| cscv-block-2-config-3 | cscv | pre_holdout_cscv_block | oco_bracket | 200 | 4 | 779.64 | 779.64 | 3.5621 | 3.5621 | 0.8905 | 0.8905 |
| cscv-block-3-config-0 | cscv | pre_holdout_cscv_block | hedge_pair | 200 | 5 | -1022.40 | -1022.40 | -2.6467 | -2.6467 | -0.5293 | -0.5293 |
| cscv-block-3-config-1 | cscv | pre_holdout_cscv_block | synthetic_breakout | 200 | 4 | -1156.70 | -1156.70 | -3.0207 | -3.0207 | -0.6753 | -0.6753 |
| cscv-block-3-config-2 | cscv | pre_holdout_cscv_block | contingent_hedge | 200 | 4 | -1630.70 | -1630.70 | -4.2215 | -4.2215 | -0.9755 | -0.9755 |
| cscv-block-3-config-3 | cscv | pre_holdout_cscv_block | oco_bracket | 200 | 5 | -976.84 | -976.84 | -2.3274 | -2.3274 | -0.4655 | -0.4655 |
| cscv-block-4-config-0 | cscv | pre_holdout_cscv_block | hedge_pair | 200 | 4 | -382.30 | -382.30 | -1.9328 | -1.9328 | -0.4376 | -0.4376 |
| cscv-block-4-config-1 | cscv | pre_holdout_cscv_block | synthetic_breakout | 200 | 2 | -224.00 | -224.00 | -1.5019 | -1.5019 | -0.8752 | -0.8752 |
| cscv-block-4-config-2 | cscv | pre_holdout_cscv_block | contingent_hedge | 200 | 1 | -281.90 | -281.90 | -0.5084 | -0.5084 | 0.2430 | 0.2430 |
| cscv-block-4-config-3 | cscv | pre_holdout_cscv_block | oco_bracket | 200 | 3 | -101.01 | -101.01 | -0.6766 | -0.6766 | -0.4645 | -0.4645 |
| cscv-block-5-config-0 | cscv | pre_holdout_cscv_block | hedge_pair | 200 | 4 | 397.00 | 397.00 | 0.9833 | 0.9833 | 0.2458 | 0.2458 |
| cscv-block-5-config-1 | cscv | pre_holdout_cscv_block | synthetic_breakout | 200 | 3 | 742.90 | 742.90 | 1.5575 | 1.5575 | 0.5192 | 0.5192 |
| cscv-block-5-config-2 | cscv | pre_holdout_cscv_block | contingent_hedge | 200 | 3 | 445.60 | 445.60 | 0.5575 | 0.5575 | 0.1858 | 0.1858 |
| cscv-block-5-config-3 | cscv | pre_holdout_cscv_block | oco_bracket | 200 | 3 | -739.84 | -739.84 | -2.3819 | -2.3819 | -1.0000 | -1.0000 |
| cscv-block-6-config-0 | cscv | pre_holdout_cscv_block | hedge_pair | 200 | 5 | 1012.00 | 1012.00 | 3.3117 | 3.3117 | 0.6798 | 0.6798 |
| cscv-block-6-config-1 | cscv | pre_holdout_cscv_block | synthetic_breakout | 200 | 3 | 654.20 | 654.20 | 1.7930 | 1.7930 | 0.6268 | 0.6268 |
| cscv-block-6-config-2 | cscv | pre_holdout_cscv_block | contingent_hedge | 200 | 3 | 372.50 | 372.50 | 0.7330 | 0.7330 | 0.2735 | 0.2735 |
| cscv-block-6-config-3 | cscv | pre_holdout_cscv_block | oco_bracket | 200 | 4 | 907.25 | 907.25 | 3.2732 | 3.2732 | 0.6217 | 0.6217 |
| cscv-block-7-config-0 | cscv | pre_holdout_cscv_block | hedge_pair | 200 | 5 | -1323.60 | -1323.60 | -4.7984 | -4.7984 | -0.9597 | -0.9597 |
| cscv-block-7-config-1 | cscv | pre_holdout_cscv_block | synthetic_breakout | 200 | 4 | -1067.60 | -1067.60 | -3.7137 | -3.7137 | -0.9284 | -0.9284 |
| cscv-block-7-config-2 | cscv | pre_holdout_cscv_block | contingent_hedge | 200 | 4 | -1754.20 | -1754.20 | -6.6988 | -6.6988 | -1.6747 | -1.6747 |
| cscv-block-7-config-3 | cscv | pre_holdout_cscv_block | oco_bracket | 200 | 3 | -8.03 | -8.03 | -0.4324 | -0.4324 | -0.2330 | -0.2330 |
