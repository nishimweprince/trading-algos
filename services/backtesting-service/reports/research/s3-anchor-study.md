# S3 anchor study

The v3 §4.1 anchor grid, one anchor at a time, as the only session, on one candle set with one configuration. Expansion ratios compare the opening range and its tick volume with the equal-length window immediately before the anchor: a ratio near `1.0` means the anchor marks nothing in particular.

**Primary question**: is New York's negative result an anchor problem? The four New York rows below answer it descriptively for this window. No anchor is selected here.

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
| Entry mode | hedge_pair |
| Anchor variants | 9 |
| Expansion baseline | equal_length_window_before_the_anchor |

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

## Every anchor variant

| Session | Anchor | Spec | Incumbent | Basis | Signals | Drift skips | Drift p50 min | Completed | Gross pips | Net pips | Gross R | Net R | Gross exp pips | Net exp pips | Gross exp R | Net exp R | Gross PF | Net PF | Survivor TP | Required TP | Margin pp | CI low | CI high | Net maxDD R | Median ORB pips | Median range expansion | Median volume expansion | Suppressed | Unresolved |
|---|---|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| tokyo | tokyo_0900 | `Asia/Tokyo:09:00` | yes | incumbent | 21 | 0 | 0.00 | 14 | 541.10 | 541.10 | 1.5103 | 1.5103 | 38.65 | 38.65 | 0.1079 | 0.1079 | 1.3861 | 1.3861 | 14.29% | 9.40% | 4.89 | -5.39 | 30.54 | 3.8823 | 205.80 | 1.7583 | 1.6485 | 7 | 0 |
| tokyo | tokyo_0845 | `Asia/Tokyo:08:45` | no | JPX/TOCOM gold day session open | 21 | 0 | 0.00 | 13 | 1067.70 | 1067.70 | 2.8005 | 2.8005 | 82.13 | 82.13 | 0.2154 | 0.2154 | 2.3170 | 2.3170 | 15.38% | 5.17% | 10.21 | -0.84 | 37.06 | 2.4386 | 195.20 | 2.0679 | 1.6931 | 8 | 0 |
| london | london_0800 | `Europe/London:08:00` | yes | LBMA market-making hours begin | 22 | 0 | 0.00 | 19 | -840.40 | -840.40 | -4.3803 | -4.3803 | -44.23 | -44.23 | -0.2305 | -0.2305 | 0.6960 | 0.6960 | 15.79% | 24.49% | -8.70 | -18.97 | 13.07 | 8.1115 | 127.45 | 0.7430 | 0.9036 | 3 | 0 |
| london | london_1030 | `Europe/London:10:30` | no | LBMA Gold Price AM auction | 22 | 0 | 0.00 | 21 | -2407.80 | -2407.80 | -8.1370 | -8.1370 | -114.66 | -114.66 | -0.3875 | -0.3875 | 0.2996 | 0.2996 | 14.29% | 28.20% | -13.91 | -23.22 | 6.44 | 9.6679 | 96.15 | 0.9338 | 0.9621 | 1 | 0 |
| london | london_1500 | `Europe/London:15:00` | no | LBMA Gold Price PM auction | 22 | 0 | 0.00 | 12 | 1199.50 | 1199.50 | 4.2898 | 4.2898 | 99.96 | 99.96 | 0.3575 | 0.3575 | 4.3422 | 4.3422 | 8.33% | 9.43% | -1.09 | -7.94 | 25.96 | 1.1734 | 218.00 | 0.7650 | 1.0071 | 9 | 1 |
| new_york | new_york_0800 | `America/New_York:08:00` | yes | incumbent, and the suspect | 22 | 0 | 0.00 | 15 | 1027.00 | 1027.00 | 4.1611 | 4.1611 | 68.47 | 68.47 | 0.2774 | 0.2774 | 2.1053 | 2.1053 | 20.00% | 7.12% | 12.88 | -0.07 | 38.07 | 1.8909 | 211.50 | 1.5423 | 1.2397 | 6 | 1 |
| new_york | new_york_0820 | `America/New_York:08:20` | no | COMEX open-outcry reference | 22 | 0 | 10.00 | 15 | 1597.80 | 1597.80 | 4.6277 | 4.6277 | 106.52 | 106.52 | 0.3085 | 0.3085 | 3.4045 | 3.4045 | 13.33% | 2.36% | 10.98 | 1.38 | 35.52 | 3.3591 | 190.45 | 1.2805 | 1.2147 | 6 | 1 |
| new_york | new_york_0830 | `America/New_York:08:30` | no | US tier-1 data window | 22 | 0 | 0.00 | 15 | 1597.80 | 1597.80 | 4.6277 | 4.6277 | 106.52 | 106.52 | 0.3085 | 0.3085 | 3.4045 | 3.4045 | 13.33% | 2.36% | 10.98 | 1.38 | 35.52 | 3.3591 | 190.45 | 1.2805 | 1.2147 | 6 | 1 |
| new_york | new_york_0930 | `America/New_York:09:30` | no | US equity open | 22 | 0 | 0.00 | 12 | 811.60 | 811.60 | 2.0244 | 2.0244 | 67.63 | 67.63 | 0.1687 | 0.1687 | 4.0592 | 4.0592 | 0.00% | 7.78% | -7.78 | -7.78 | 16.47 | 1.6870 | 262.20 | 1.3154 | 1.1766 | 9 | 1 |

## Bar-resolution degeneracy

An anchor that does not fall on a bar boundary snaps forward to the next bar open, so two anchors inside the same bar are the same experiment at this resolution. Variants whose measured results are identical are listed here; treat them as one observation, not as agreement between two anchors.

| Session | Collapsed variants | Signals | Completed | Net R |
|---|---|---:|---:|---:|
| new_york | new_york_0820, new_york_0830 | 22 | 15 | 4.6277 |

## Reading the New York question

- The incumbent New York anchor (`America/New_York:08:00`) produced 15 completed structures, 4.1611 net R, and a median range expansion of 1.5423.
- 2 of the 3 alternative New York anchors finished above it in net R on this window. That is a description of one month, not a reason to move the anchor; §9 requires walk-forward evidence before an anchor changes.
- Every anchor's drift statistics are reported above. An anchor whose p50 drift exceeds `ANCHOR_TOLERANCE_MINUTES` would be void rather than underperforming, which is the H4 lesson from §0.2.

## Caveats

- 2000 M15 bars from 2026-07-21T05:45:00+00:00 to 2026-08-19T23:30:00+00:00: roughly twenty trading days per anchor. Every row here is a small sample.
- M1 bars were present but covered only 93 of 2000 parent bars (4.65% of the window). Mixing M1 chronology on part of the window with the fallback on the rest would make results inside one study incomparable, so no M1 chronology was used: the whole window was resolved with the conservative pessimistic_same_bar_no_subpath fallback, in which a bar touching both the stop and the target is taken as the stop.
- Each variant runs as the only session, so concurrency and the one-open-per-session gate cannot interact across sessions. That isolates the anchor and makes these rows incomparable with a three-session run.
- Expansion ratios use tick volume as reported by the data provider. They describe activity around the anchor, not spread or liquidity, which the local cache does not carry.
