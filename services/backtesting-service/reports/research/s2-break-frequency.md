# S2 single-break versus double-break frequency

Once the opening range closes, does price leave one side and never test the other? The single-break share is the ceiling on any one-sided breakout; the double-break share is what the `hedge_pair` whipsaw and the `oco_bracket` false break are drawn from.

Classes are mutually exclusive and exhaust every episode at every horizon: `no_break`, `single_break_up`, `single_break_down`, `double_break_up_first`, `double_break_down_first`, and `ambiguous_same_bar` for a bar that breaks both sides at once. Ambiguous bars are counted with the double breaks in the double-break rate and are also reported on their own, because without an M1 subpath there is no honest way to order the two touches.

The walk starts when the opening range closes, not at the entry time, so the answer describes the range rather than `ENTRY_DELAY_MINUTES`.

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
| Sessions | tokyo, london, new_york |
| Horizons (hours) | 4, 8, 12, 24, 48 |
| Walk starts at | opening_range_close |
| Episodes | 65 |
| Episodes without forward bars | 0 |
| Contraction tercile edges (ORB pips / ATR pips) | 2.1471, 2.9939 |

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

## Break classes, every group and horizon

| Group | Key | Horizon h | n | No break | Single up | Single down | Double up first | Double down first | Ambiguous same bar | Single rate | CI low | CI high | Double rate | CI low | CI high | No-break rate | Median first break h | Median opposite break h |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| all | all | 4 | 65 | 1 | 21 | 23 | 8 | 11 | 1 | 67.69% | 55.61% | 77.80% | 30.77% | 20.89% | 42.80% | 1.54% | 0.25 | 0.75 |
| all | all | 8 | 65 | 1 | 18 | 16 | 11 | 18 | 1 | 52.31% | 40.38% | 63.98% | 46.15% | 34.59% | 58.15% | 1.54% | 0.25 | 1.38 |
| all | all | 12 | 65 | 1 | 18 | 12 | 11 | 22 | 1 | 46.15% | 34.59% | 58.15% | 52.31% | 40.38% | 63.98% | 1.54% | 0.25 | 2.25 |
| all | all | 24 | 65 | 1 | 16 | 11 | 13 | 23 | 1 | 41.54% | 30.36% | 53.66% | 56.92% | 44.83% | 68.24% | 1.54% | 0.25 | 2.50 |
| all | all | 48 | 65 | 1 | 11 | 9 | 18 | 25 | 1 | 30.77% | 20.89% | 42.80% | 67.69% | 55.61% | 77.80% | 1.54% | 0.25 | 4.88 |
| session | london | 4 | 22 | 0 | 9 | 9 | 1 | 2 | 1 | 81.82% | 61.48% | 92.69% | 18.18% | 7.31% | 38.52% | 0.00% | 0.25 | 1.00 |
| session | london | 8 | 22 | 0 | 6 | 6 | 4 | 5 | 1 | 54.55% | 34.66% | 73.08% | 45.45% | 26.92% | 65.34% | 0.00% | 0.25 | 4.88 |
| session | london | 12 | 22 | 0 | 6 | 4 | 4 | 7 | 1 | 45.45% | 26.92% | 65.34% | 54.55% | 34.66% | 73.08% | 0.00% | 0.25 | 5.00 |
| session | london | 24 | 22 | 0 | 6 | 3 | 4 | 8 | 1 | 40.91% | 23.26% | 61.27% | 59.09% | 38.73% | 76.74% | 0.00% | 0.25 | 5.00 |
| session | london | 48 | 22 | 0 | 5 | 3 | 5 | 8 | 1 | 36.36% | 19.73% | 57.05% | 63.64% | 42.95% | 80.27% | 0.00% | 0.25 | 5.38 |
| session | new_york | 4 | 22 | 1 | 7 | 5 | 3 | 6 | 0 | 54.55% | 34.66% | 73.08% | 40.91% | 23.26% | 61.27% | 4.55% | 0.25 | 1.00 |
| session | new_york | 8 | 22 | 1 | 7 | 4 | 3 | 7 | 0 | 50.00% | 30.72% | 69.28% | 45.45% | 26.92% | 65.34% | 4.55% | 0.25 | 1.00 |
| session | new_york | 12 | 22 | 1 | 7 | 3 | 3 | 8 | 0 | 45.45% | 26.92% | 65.34% | 50.00% | 30.72% | 69.28% | 4.55% | 0.25 | 1.00 |
| session | new_york | 24 | 22 | 1 | 5 | 3 | 5 | 8 | 0 | 36.36% | 19.73% | 57.05% | 59.09% | 38.73% | 76.74% | 4.55% | 0.25 | 1.25 |
| session | new_york | 48 | 22 | 1 | 4 | 3 | 6 | 8 | 0 | 31.82% | 16.36% | 52.68% | 63.64% | 42.95% | 80.27% | 4.55% | 0.25 | 1.25 |
| session | tokyo | 4 | 21 | 0 | 5 | 9 | 4 | 3 | 0 | 66.67% | 45.37% | 82.81% | 33.33% | 17.19% | 54.63% | 0.00% | 0.25 | 0.75 |
| session | tokyo | 8 | 21 | 0 | 5 | 6 | 4 | 6 | 0 | 52.38% | 32.37% | 71.66% | 47.62% | 28.34% | 67.63% | 0.00% | 0.25 | 1.38 |
| session | tokyo | 12 | 21 | 0 | 5 | 5 | 4 | 7 | 0 | 47.62% | 28.34% | 67.63% | 52.38% | 32.37% | 71.66% | 0.00% | 0.25 | 2.00 |
| session | tokyo | 24 | 21 | 0 | 5 | 5 | 4 | 7 | 0 | 47.62% | 28.34% | 67.63% | 52.38% | 32.37% | 71.66% | 0.00% | 0.25 | 2.00 |
| session | tokyo | 48 | 21 | 0 | 2 | 3 | 7 | 9 | 0 | 23.81% | 10.63% | 45.09% | 76.19% | 54.91% | 89.37% | 0.00% | 0.25 | 5.75 |
| weekday | monday | 4 | 12 | 0 | 2 | 9 | 1 | 0 | 0 | 91.67% | 64.61% | 98.51% | 8.33% | 1.49% | 35.39% | 0.00% | 0.25 | 1.00 |
| weekday | monday | 8 | 12 | 0 | 2 | 7 | 1 | 2 | 0 | 75.00% | 46.77% | 91.11% | 25.00% | 8.89% | 53.23% | 0.00% | 0.25 | 5.00 |
| weekday | monday | 12 | 12 | 0 | 2 | 5 | 1 | 4 | 0 | 58.33% | 31.95% | 80.67% | 41.67% | 19.33% | 68.05% | 0.00% | 0.25 | 6.00 |
| weekday | monday | 24 | 12 | 0 | 2 | 4 | 1 | 5 | 0 | 50.00% | 25.38% | 74.62% | 50.00% | 25.38% | 74.62% | 0.00% | 0.25 | 7.12 |
| weekday | monday | 48 | 12 | 0 | 0 | 3 | 3 | 6 | 0 | 25.00% | 8.89% | 53.23% | 75.00% | 46.77% | 91.11% | 0.00% | 0.25 | 11.25 |
| weekday | tuesday | 4 | 14 | 0 | 2 | 4 | 3 | 4 | 1 | 42.86% | 21.38% | 67.41% | 57.14% | 32.59% | 78.62% | 0.00% | 0.25 | 0.75 |
| weekday | tuesday | 8 | 14 | 0 | 2 | 1 | 3 | 7 | 1 | 21.43% | 7.57% | 47.59% | 78.57% | 52.41% | 92.43% | 0.00% | 0.25 | 1.25 |
| weekday | tuesday | 12 | 14 | 0 | 2 | 1 | 3 | 7 | 1 | 21.43% | 7.57% | 47.59% | 78.57% | 52.41% | 92.43% | 0.00% | 0.25 | 1.25 |
| weekday | tuesday | 24 | 14 | 0 | 2 | 1 | 3 | 7 | 1 | 21.43% | 7.57% | 47.59% | 78.57% | 52.41% | 92.43% | 0.00% | 0.25 | 1.25 |
| weekday | tuesday | 48 | 14 | 0 | 2 | 0 | 3 | 8 | 1 | 14.29% | 4.01% | 39.94% | 85.71% | 60.06% | 95.99% | 0.00% | 0.25 | 1.25 |
| weekday | wednesday | 4 | 15 | 0 | 10 | 2 | 0 | 3 | 0 | 80.00% | 54.81% | 92.95% | 20.00% | 7.05% | 45.19% | 0.00% | 0.25 | 0.75 |
| weekday | wednesday | 8 | 15 | 0 | 8 | 1 | 2 | 4 | 0 | 60.00% | 35.75% | 80.18% | 40.00% | 19.82% | 64.25% | 0.00% | 0.25 | 3.88 |
| weekday | wednesday | 12 | 15 | 0 | 8 | 0 | 2 | 5 | 0 | 53.33% | 30.12% | 75.19% | 46.67% | 24.81% | 69.88% | 0.00% | 0.25 | 4.25 |
| weekday | wednesday | 24 | 15 | 0 | 6 | 0 | 4 | 5 | 0 | 40.00% | 19.82% | 64.25% | 60.00% | 35.75% | 80.18% | 0.00% | 0.25 | 4.75 |
| weekday | wednesday | 48 | 15 | 0 | 4 | 0 | 6 | 5 | 0 | 26.67% | 10.90% | 51.95% | 73.33% | 48.05% | 89.10% | 0.00% | 0.25 | 5.00 |
| weekday | thursday | 4 | 12 | 0 | 2 | 3 | 4 | 3 | 0 | 41.67% | 19.33% | 68.05% | 58.33% | 31.95% | 80.67% | 0.00% | 0.25 | 0.75 |
| weekday | thursday | 8 | 12 | 0 | 1 | 3 | 5 | 3 | 0 | 33.33% | 13.81% | 60.94% | 66.67% | 39.06% | 86.19% | 0.00% | 0.25 | 1.12 |
| weekday | thursday | 12 | 12 | 0 | 1 | 3 | 5 | 3 | 0 | 33.33% | 13.81% | 60.94% | 66.67% | 39.06% | 86.19% | 0.00% | 0.25 | 1.12 |
| weekday | thursday | 24 | 12 | 0 | 1 | 3 | 5 | 3 | 0 | 33.33% | 13.81% | 60.94% | 66.67% | 39.06% | 86.19% | 0.00% | 0.25 | 1.12 |
| weekday | thursday | 48 | 12 | 0 | 0 | 3 | 6 | 3 | 0 | 25.00% | 8.89% | 53.23% | 75.00% | 46.77% | 91.11% | 0.00% | 0.25 | 1.50 |
| weekday | friday | 4 | 12 | 1 | 5 | 5 | 0 | 1 | 0 | 83.33% | 55.20% | 95.30% | 8.33% | 1.49% | 35.39% | 8.33% | 0.25 | 1.00 |
| weekday | friday | 8 | 12 | 1 | 5 | 4 | 0 | 2 | 0 | 75.00% | 46.77% | 91.11% | 16.67% | 4.70% | 44.80% | 8.33% | 0.25 | 3.75 |
| weekday | friday | 12 | 12 | 1 | 5 | 3 | 0 | 3 | 0 | 66.67% | 39.06% | 86.19% | 25.00% | 8.89% | 53.23% | 8.33% | 0.25 | 6.50 |
| weekday | friday | 24 | 12 | 1 | 5 | 3 | 0 | 3 | 0 | 66.67% | 39.06% | 86.19% | 25.00% | 8.89% | 53.23% | 8.33% | 0.25 | 6.50 |
| weekday | friday | 48 | 12 | 1 | 5 | 3 | 0 | 3 | 0 | 66.67% | 39.06% | 86.19% | 25.00% | 8.89% | 53.23% | 8.33% | 0.25 | 6.50 |
| contraction_tercile | low | 4 | 22 | 0 | 7 | 6 | 2 | 6 | 1 | 59.09% | 38.73% | 76.74% | 40.91% | 23.26% | 61.27% | 0.00% | 0.25 | 0.75 |
| contraction_tercile | low | 8 | 22 | 0 | 4 | 4 | 5 | 8 | 1 | 36.36% | 19.73% | 57.05% | 63.64% | 42.95% | 80.27% | 0.00% | 0.25 | 2.00 |
| contraction_tercile | low | 12 | 22 | 0 | 4 | 2 | 5 | 10 | 1 | 27.27% | 13.15% | 48.15% | 72.73% | 51.85% | 86.85% | 0.00% | 0.25 | 3.00 |
| contraction_tercile | low | 24 | 22 | 0 | 4 | 1 | 5 | 11 | 1 | 22.73% | 10.12% | 43.44% | 77.27% | 56.56% | 89.88% | 0.00% | 0.25 | 3.50 |
| contraction_tercile | low | 48 | 22 | 0 | 3 | 1 | 6 | 11 | 1 | 18.18% | 7.31% | 38.52% | 81.82% | 61.48% | 92.69% | 0.00% | 0.25 | 4.12 |
| contraction_tercile | mid | 4 | 21 | 0 | 7 | 6 | 4 | 4 | 0 | 61.90% | 40.88% | 79.25% | 38.10% | 20.75% | 59.12% | 0.00% | 0.25 | 0.88 |
| contraction_tercile | mid | 8 | 21 | 0 | 7 | 4 | 4 | 6 | 0 | 52.38% | 32.37% | 71.66% | 47.62% | 28.34% | 67.63% | 0.00% | 0.25 | 1.12 |
| contraction_tercile | mid | 12 | 21 | 0 | 7 | 4 | 4 | 6 | 0 | 52.38% | 32.37% | 71.66% | 47.62% | 28.34% | 67.63% | 0.00% | 0.25 | 1.12 |
| contraction_tercile | mid | 24 | 21 | 0 | 7 | 4 | 4 | 6 | 0 | 52.38% | 32.37% | 71.66% | 47.62% | 28.34% | 67.63% | 0.00% | 0.25 | 1.12 |
| contraction_tercile | mid | 48 | 21 | 0 | 5 | 3 | 6 | 7 | 0 | 38.10% | 20.75% | 59.12% | 61.90% | 40.88% | 79.25% | 0.00% | 0.25 | 1.25 |
| contraction_tercile | high | 4 | 21 | 1 | 7 | 10 | 2 | 1 | 0 | 80.95% | 60.00% | 92.33% | 14.29% | 4.98% | 34.64% | 4.76% | 0.25 | 0.75 |
| contraction_tercile | high | 8 | 21 | 1 | 7 | 8 | 2 | 3 | 0 | 71.43% | 50.04% | 86.19% | 23.81% | 10.63% | 45.09% | 4.76% | 0.25 | 2.50 |
| contraction_tercile | high | 12 | 21 | 1 | 7 | 6 | 2 | 5 | 0 | 61.90% | 40.88% | 79.25% | 33.33% | 17.19% | 54.63% | 4.76% | 0.25 | 4.25 |
| contraction_tercile | high | 24 | 21 | 1 | 5 | 6 | 4 | 5 | 0 | 52.38% | 32.37% | 71.66% | 42.86% | 24.47% | 63.45% | 4.76% | 0.25 | 7.25 |
| contraction_tercile | high | 48 | 21 | 1 | 3 | 5 | 6 | 6 | 0 | 38.10% | 20.75% | 59.12% | 57.14% | 36.55% | 75.53% | 4.76% | 0.25 | 11.00 |
| contraction_tercile | unclassified | 4 | 1 | 0 | 0 | 1 | 0 | 0 | 0 | 100.00% | 20.65% | 100.00% | 0.00% | 0.00% | 79.35% | 0.00% | 0.25 | — |
| contraction_tercile | unclassified | 8 | 1 | 0 | 0 | 0 | 0 | 1 | 0 | 0.00% | 0.00% | 79.35% | 100.00% | 20.65% | 100.00% | 0.00% | 0.25 | 7.75 |
| contraction_tercile | unclassified | 12 | 1 | 0 | 0 | 0 | 0 | 1 | 0 | 0.00% | 0.00% | 79.35% | 100.00% | 20.65% | 100.00% | 0.00% | 0.25 | 7.75 |
| contraction_tercile | unclassified | 24 | 1 | 0 | 0 | 0 | 0 | 1 | 0 | 0.00% | 0.00% | 79.35% | 100.00% | 20.65% | 100.00% | 0.00% | 0.25 | 7.75 |
| contraction_tercile | unclassified | 48 | 1 | 0 | 0 | 0 | 0 | 1 | 0 | 0.00% | 0.00% | 79.35% | 100.00% | 20.65% | 100.00% | 0.00% | 0.25 | 7.75 |

## What the engine actually paid for these breaks

`false_break_rate` is triggered structures that closed at a loss, divided by triggered structures; entry orders that expired or were cancelled without triggering are excluded. It is a proxy, not a claim about intent: a triggered structure can lose for reasons other than a failed break.

`Cancelled` counts every `entry_order_cancelled` event, which includes the sibling order cancelled when the other side of a two-sided entry fills. `Expired` is the subset that timed out without ever filling, and only that subset means "no break arrived in time".

| Mode | Completed | Whipsaw | Whipsaw rate | CI low | CI high | TP | Lock | Breakeven | Time exit | Triggered | Cancelled | Expired | Loss-closed | False-break rate | Gross pips | Net pips | Gross R | Net R |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| hedge_pair | 48 | 1 | 2.08% | 0.37% | 10.90% | 8 | 17 | 0 | 22 | 49 | 0 | 0 | 24 | 48.98% | 727.70 | 727.70 | 1.2911 | 1.2911 |
| synthetic_breakout | 37 | 0 | 0.00% | 0.00% | 9.41% | 7 | 18 | 0 | 12 | 38 | 38 | 0 | 22 | 57.89% | 721.40 | 721.40 | 1.0187 | 1.0187 |
| contingent_hedge | 34 | 8 | 23.53% | 12.44% | 40.00% | 4 | 3 | 0 | 19 | 35 | 35 | 0 | 15 | 42.86% | 1181.90 | 1181.90 | 0.8555 | 0.8555 |
| oco_bracket | 40 | 0 | 0.00% | 0.00% | 8.76% | 4 | 18 | 0 | 18 | 41 | 51 | 10 | 21 | 51.22% | 2027.83 | 2027.83 | 6.2044 | 6.2044 |

## Every episode at every horizon

| Session | Anchor | Weekday | Horizon h | ORB pips | ATR pips | ORB/ATR | Contraction tercile | Signal | Class | First break | First break h | Opposite break h | Fwd bars |
|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| london | 2026-07-21T08:00:00+01:00 | tuesday | 4 | 123.30 | — | — | unclassified | down | single_break_down | down | 0.25 | — | 17 |
| london | 2026-07-21T08:00:00+01:00 | tuesday | 8 | 123.30 | — | — | unclassified | down | double_break_down_first | down | 0.25 | 7.75 | 32 |
| london | 2026-07-21T08:00:00+01:00 | tuesday | 12 | 123.30 | — | — | unclassified | down | double_break_down_first | down | 0.25 | 7.75 | 32 |
| london | 2026-07-21T08:00:00+01:00 | tuesday | 24 | 123.30 | — | — | unclassified | down | double_break_down_first | down | 0.25 | 7.75 | 32 |
| london | 2026-07-21T08:00:00+01:00 | tuesday | 48 | 123.30 | — | — | unclassified | down | double_break_down_first | down | 0.25 | 7.75 | 32 |
| new_york | 2026-07-21T08:00:00-04:00 | tuesday | 4 | 144.00 | 64.79 | 2.2227 | mid | down | double_break_down_first | down | 0.50 | 1.25 | 6 |
| new_york | 2026-07-21T08:00:00-04:00 | tuesday | 8 | 144.00 | 64.79 | 2.2227 | mid | down | double_break_down_first | down | 0.50 | 1.25 | 6 |
| new_york | 2026-07-21T08:00:00-04:00 | tuesday | 12 | 144.00 | 64.79 | 2.2227 | mid | down | double_break_down_first | down | 0.50 | 1.25 | 6 |
| new_york | 2026-07-21T08:00:00-04:00 | tuesday | 24 | 144.00 | 64.79 | 2.2227 | mid | down | double_break_down_first | down | 0.50 | 1.25 | 6 |
| new_york | 2026-07-21T08:00:00-04:00 | tuesday | 48 | 144.00 | 64.79 | 2.2227 | mid | down | double_break_down_first | down | 0.50 | 1.25 | 6 |
| tokyo | 2026-07-22T09:00:00+09:00 | wednesday | 4 | 253.70 | 55.87 | 4.5408 | high | up | single_break_up | up | 0.25 | — | 17 |
| tokyo | 2026-07-22T09:00:00+09:00 | wednesday | 8 | 253.70 | 55.87 | 4.5408 | high | up | single_break_up | up | 0.25 | — | 33 |
| tokyo | 2026-07-22T09:00:00+09:00 | wednesday | 12 | 253.70 | 55.87 | 4.5408 | high | up | single_break_up | up | 0.25 | — | 49 |
| tokyo | 2026-07-22T09:00:00+09:00 | wednesday | 24 | 253.70 | 55.87 | 4.5408 | high | up | single_break_up | up | 0.25 | — | 93 |
| tokyo | 2026-07-22T09:00:00+09:00 | wednesday | 48 | 253.70 | 55.87 | 4.5408 | high | up | double_break_up_first | up | 0.25 | 34.75 | 136 |
| london | 2026-07-22T08:00:00+01:00 | wednesday | 4 | 137.30 | 68.95 | 1.9913 | low | down | single_break_up | up | 4.00 | — | 17 |
| london | 2026-07-22T08:00:00+01:00 | wednesday | 8 | 137.30 | 68.95 | 1.9913 | low | down | double_break_up_first | up | 4.00 | 5.00 | 21 |
| london | 2026-07-22T08:00:00+01:00 | wednesday | 12 | 137.30 | 68.95 | 1.9913 | low | down | double_break_up_first | up | 4.00 | 5.00 | 21 |
| london | 2026-07-22T08:00:00+01:00 | wednesday | 24 | 137.30 | 68.95 | 1.9913 | low | down | double_break_up_first | up | 4.00 | 5.00 | 21 |
| london | 2026-07-22T08:00:00+01:00 | wednesday | 48 | 137.30 | 68.95 | 1.9913 | low | down | double_break_up_first | up | 4.00 | 5.00 | 21 |
| new_york | 2026-07-22T08:00:00-04:00 | wednesday | 4 | 268.90 | 71.97 | 3.7362 | high | down | single_break_up | up | 0.50 | — | 17 |
| new_york | 2026-07-22T08:00:00-04:00 | wednesday | 8 | 268.90 | 71.97 | 3.7362 | high | down | single_break_up | up | 0.50 | — | 33 |
| new_york | 2026-07-22T08:00:00-04:00 | wednesday | 12 | 268.90 | 71.97 | 3.7362 | high | down | single_break_up | up | 0.50 | — | 45 |
| new_york | 2026-07-22T08:00:00-04:00 | wednesday | 24 | 268.90 | 71.97 | 3.7362 | high | down | double_break_up_first | up | 0.50 | 18.25 | 70 |
| new_york | 2026-07-22T08:00:00-04:00 | wednesday | 48 | 268.90 | 71.97 | 3.7362 | high | down | double_break_up_first | up | 0.50 | 18.25 | 70 |
| tokyo | 2026-07-23T09:00:00+09:00 | thursday | 4 | 95.30 | 74.20 | 1.2844 | low | down | double_break_up_first | up | 0.25 | 0.75 | 4 |
| tokyo | 2026-07-23T09:00:00+09:00 | thursday | 8 | 95.30 | 74.20 | 1.2844 | low | down | double_break_up_first | up | 0.25 | 0.75 | 4 |
| tokyo | 2026-07-23T09:00:00+09:00 | thursday | 12 | 95.30 | 74.20 | 1.2844 | low | down | double_break_up_first | up | 0.25 | 0.75 | 4 |
| tokyo | 2026-07-23T09:00:00+09:00 | thursday | 24 | 95.30 | 74.20 | 1.2844 | low | down | double_break_up_first | up | 0.25 | 0.75 | 4 |
| tokyo | 2026-07-23T09:00:00+09:00 | thursday | 48 | 95.30 | 74.20 | 1.2844 | low | down | double_break_up_first | up | 0.25 | 0.75 | 4 |
| london | 2026-07-23T08:00:00+01:00 | thursday | 4 | 290.30 | 77.79 | 3.7317 | high | down | single_break_down | down | 0.25 | — | 17 |
| london | 2026-07-23T08:00:00+01:00 | thursday | 8 | 290.30 | 77.79 | 3.7317 | high | down | single_break_down | down | 0.25 | — | 33 |
| london | 2026-07-23T08:00:00+01:00 | thursday | 12 | 290.30 | 77.79 | 3.7317 | high | down | single_break_down | down | 0.25 | — | 49 |
| london | 2026-07-23T08:00:00+01:00 | thursday | 24 | 290.30 | 77.79 | 3.7317 | high | down | single_break_down | down | 0.25 | — | 93 |
| london | 2026-07-23T08:00:00+01:00 | thursday | 48 | 290.30 | 77.79 | 3.7317 | high | down | single_break_down | down | 0.25 | — | 145 |
| new_york | 2026-07-23T08:00:00-04:00 | thursday | 4 | 248.20 | 76.94 | 3.2258 | high | down | single_break_down | down | 0.25 | — | 17 |
| new_york | 2026-07-23T08:00:00-04:00 | thursday | 8 | 248.20 | 76.94 | 3.2258 | high | down | single_break_down | down | 0.25 | — | 33 |
| new_york | 2026-07-23T08:00:00-04:00 | thursday | 12 | 248.20 | 76.94 | 3.2258 | high | down | single_break_down | down | 0.25 | — | 45 |
| new_york | 2026-07-23T08:00:00-04:00 | thursday | 24 | 248.20 | 76.94 | 3.2258 | high | down | single_break_down | down | 0.25 | — | 93 |
| new_york | 2026-07-23T08:00:00-04:00 | thursday | 48 | 248.20 | 76.94 | 3.2258 | high | down | single_break_down | down | 0.25 | — | 125 |
| tokyo | 2026-07-24T09:00:00+09:00 | friday | 4 | 93.80 | 32.66 | 2.8723 | mid | up | single_break_down | down | 0.25 | — | 17 |
| tokyo | 2026-07-24T09:00:00+09:00 | friday | 8 | 93.80 | 32.66 | 2.8723 | mid | up | double_break_down_first | down | 0.25 | 6.50 | 27 |
| tokyo | 2026-07-24T09:00:00+09:00 | friday | 12 | 93.80 | 32.66 | 2.8723 | mid | up | double_break_down_first | down | 0.25 | 6.50 | 27 |
| tokyo | 2026-07-24T09:00:00+09:00 | friday | 24 | 93.80 | 32.66 | 2.8723 | mid | up | double_break_down_first | down | 0.25 | 6.50 | 27 |
| tokyo | 2026-07-24T09:00:00+09:00 | friday | 48 | 93.80 | 32.66 | 2.8723 | mid | up | double_break_down_first | down | 0.25 | 6.50 | 27 |
| london | 2026-07-24T08:00:00+01:00 | friday | 4 | 184.80 | 67.87 | 2.7228 | mid | up | single_break_up | up | 0.75 | — | 17 |
| london | 2026-07-24T08:00:00+01:00 | friday | 8 | 184.80 | 67.87 | 2.7228 | mid | up | single_break_up | up | 0.75 | — | 33 |
| london | 2026-07-24T08:00:00+01:00 | friday | 12 | 184.80 | 67.87 | 2.7228 | mid | up | single_break_up | up | 0.75 | — | 49 |
| london | 2026-07-24T08:00:00+01:00 | friday | 24 | 184.80 | 67.87 | 2.7228 | mid | up | single_break_up | up | 0.75 | — | 53 |
| london | 2026-07-24T08:00:00+01:00 | friday | 48 | 184.80 | 67.87 | 2.7228 | mid | up | single_break_up | up | 0.75 | — | 53 |
| new_york | 2026-07-24T08:00:00-04:00 | friday | 4 | 84.50 | 53.26 | 1.5864 | low | down | double_break_down_first | down | 0.25 | 1.00 | 5 |
| new_york | 2026-07-24T08:00:00-04:00 | friday | 8 | 84.50 | 53.26 | 1.5864 | low | down | double_break_down_first | down | 0.25 | 1.00 | 5 |
| new_york | 2026-07-24T08:00:00-04:00 | friday | 12 | 84.50 | 53.26 | 1.5864 | low | down | double_break_down_first | down | 0.25 | 1.00 | 5 |
| new_york | 2026-07-24T08:00:00-04:00 | friday | 24 | 84.50 | 53.26 | 1.5864 | low | down | double_break_down_first | down | 0.25 | 1.00 | 5 |
| new_york | 2026-07-24T08:00:00-04:00 | friday | 48 | 84.50 | 53.26 | 1.5864 | low | down | double_break_down_first | down | 0.25 | 1.00 | 5 |
| tokyo | 2026-07-27T09:00:00+09:00 | monday | 4 | 254.70 | 113.26 | 2.2487 | mid | up | single_break_down | down | 2.25 | — | 17 |
| tokyo | 2026-07-27T09:00:00+09:00 | monday | 8 | 254.70 | 113.26 | 2.2487 | mid | up | single_break_down | down | 2.25 | — | 33 |
| tokyo | 2026-07-27T09:00:00+09:00 | monday | 12 | 254.70 | 113.26 | 2.2487 | mid | up | single_break_down | down | 2.25 | — | 49 |
| tokyo | 2026-07-27T09:00:00+09:00 | monday | 24 | 254.70 | 113.26 | 2.2487 | mid | up | single_break_down | down | 2.25 | — | 93 |
| tokyo | 2026-07-27T09:00:00+09:00 | monday | 48 | 254.70 | 113.26 | 2.2487 | mid | up | single_break_down | down | 2.25 | — | 185 |
| london | 2026-07-27T08:00:00+01:00 | monday | 4 | 174.00 | 59.16 | 2.9410 | mid | down | single_break_down | down | 0.25 | — | 17 |
| london | 2026-07-27T08:00:00+01:00 | monday | 8 | 174.00 | 59.16 | 2.9410 | mid | down | single_break_down | down | 0.25 | — | 33 |
| london | 2026-07-27T08:00:00+01:00 | monday | 12 | 174.00 | 59.16 | 2.9410 | mid | down | single_break_down | down | 0.25 | — | 49 |
| london | 2026-07-27T08:00:00+01:00 | monday | 24 | 174.00 | 59.16 | 2.9410 | mid | down | single_break_down | down | 0.25 | — | 93 |
| london | 2026-07-27T08:00:00+01:00 | monday | 48 | 174.00 | 59.16 | 2.9410 | mid | down | single_break_down | down | 0.25 | — | 185 |
| new_york | 2026-07-27T08:00:00-04:00 | monday | 4 | 159.00 | 64.78 | 2.4545 | mid | down | single_break_down | down | 0.25 | — | 17 |
| new_york | 2026-07-27T08:00:00-04:00 | monday | 8 | 159.00 | 64.78 | 2.4545 | mid | down | single_break_down | down | 0.25 | — | 33 |
| new_york | 2026-07-27T08:00:00-04:00 | monday | 12 | 159.00 | 64.78 | 2.4545 | mid | down | single_break_down | down | 0.25 | — | 45 |
| new_york | 2026-07-27T08:00:00-04:00 | monday | 24 | 159.00 | 64.78 | 2.4545 | mid | down | single_break_down | down | 0.25 | — | 93 |
| new_york | 2026-07-27T08:00:00-04:00 | monday | 48 | 159.00 | 64.78 | 2.4545 | mid | down | single_break_down | down | 0.25 | — | 185 |
| tokyo | 2026-07-28T09:00:00+09:00 | tuesday | 4 | 163.60 | 46.31 | 3.5324 | high | down | single_break_down | down | 0.25 | — | 17 |
| tokyo | 2026-07-28T09:00:00+09:00 | tuesday | 8 | 163.60 | 46.31 | 3.5324 | high | down | single_break_down | down | 0.25 | — | 33 |
| tokyo | 2026-07-28T09:00:00+09:00 | tuesday | 12 | 163.60 | 46.31 | 3.5324 | high | down | single_break_down | down | 0.25 | — | 49 |
| tokyo | 2026-07-28T09:00:00+09:00 | tuesday | 24 | 163.60 | 46.31 | 3.5324 | high | down | single_break_down | down | 0.25 | — | 93 |
| tokyo | 2026-07-28T09:00:00+09:00 | tuesday | 48 | 163.60 | 46.31 | 3.5324 | high | down | double_break_down_first | down | 0.25 | 41.25 | 162 |
| london | 2026-07-28T08:00:00+01:00 | tuesday | 4 | 61.70 | 47.36 | 1.3027 | low | down | ambiguous_same_bar | both | 0.25 | 0.25 | 2 |
| london | 2026-07-28T08:00:00+01:00 | tuesday | 8 | 61.70 | 47.36 | 1.3027 | low | down | ambiguous_same_bar | both | 0.25 | 0.25 | 2 |
| london | 2026-07-28T08:00:00+01:00 | tuesday | 12 | 61.70 | 47.36 | 1.3027 | low | down | ambiguous_same_bar | both | 0.25 | 0.25 | 2 |
| london | 2026-07-28T08:00:00+01:00 | tuesday | 24 | 61.70 | 47.36 | 1.3027 | low | down | ambiguous_same_bar | both | 0.25 | 0.25 | 2 |
| london | 2026-07-28T08:00:00+01:00 | tuesday | 48 | 61.70 | 47.36 | 1.3027 | low | down | ambiguous_same_bar | both | 0.25 | 0.25 | 2 |
| new_york | 2026-07-28T08:00:00-04:00 | tuesday | 4 | 153.00 | 79.10 | 1.9343 | low | up | double_break_down_first | down | 0.75 | 2.50 | 11 |
| new_york | 2026-07-28T08:00:00-04:00 | tuesday | 8 | 153.00 | 79.10 | 1.9343 | low | up | double_break_down_first | down | 0.75 | 2.50 | 11 |
| new_york | 2026-07-28T08:00:00-04:00 | tuesday | 12 | 153.00 | 79.10 | 1.9343 | low | up | double_break_down_first | down | 0.75 | 2.50 | 11 |
| new_york | 2026-07-28T08:00:00-04:00 | tuesday | 24 | 153.00 | 79.10 | 1.9343 | low | up | double_break_down_first | down | 0.75 | 2.50 | 11 |
| new_york | 2026-07-28T08:00:00-04:00 | tuesday | 48 | 153.00 | 79.10 | 1.9343 | low | up | double_break_down_first | down | 0.75 | 2.50 | 11 |
| tokyo | 2026-07-29T09:00:00+09:00 | wednesday | 4 | 117.30 | 61.34 | 1.9124 | low | down | double_break_down_first | down | 0.25 | 0.75 | 4 |
| tokyo | 2026-07-29T09:00:00+09:00 | wednesday | 8 | 117.30 | 61.34 | 1.9124 | low | down | double_break_down_first | down | 0.25 | 0.75 | 4 |
| tokyo | 2026-07-29T09:00:00+09:00 | wednesday | 12 | 117.30 | 61.34 | 1.9124 | low | down | double_break_down_first | down | 0.25 | 0.75 | 4 |
| tokyo | 2026-07-29T09:00:00+09:00 | wednesday | 24 | 117.30 | 61.34 | 1.9124 | low | down | double_break_down_first | down | 0.25 | 0.75 | 4 |
| tokyo | 2026-07-29T09:00:00+09:00 | wednesday | 48 | 117.30 | 61.34 | 1.9124 | low | down | double_break_down_first | down | 0.25 | 0.75 | 4 |
| london | 2026-07-29T08:00:00+01:00 | wednesday | 4 | 66.20 | 50.67 | 1.3065 | low | up | single_break_down | down | 0.25 | — | 17 |
| london | 2026-07-29T08:00:00+01:00 | wednesday | 8 | 66.20 | 50.67 | 1.3065 | low | up | single_break_down | down | 0.25 | — | 33 |
| london | 2026-07-29T08:00:00+01:00 | wednesday | 12 | 66.20 | 50.67 | 1.3065 | low | up | double_break_down_first | down | 0.25 | 9.25 | 38 |
| london | 2026-07-29T08:00:00+01:00 | wednesday | 24 | 66.20 | 50.67 | 1.3065 | low | up | double_break_down_first | down | 0.25 | 9.25 | 38 |
| london | 2026-07-29T08:00:00+01:00 | wednesday | 48 | 66.20 | 50.67 | 1.3065 | low | up | double_break_down_first | down | 0.25 | 9.25 | 38 |
| new_york | 2026-07-29T08:00:00-04:00 | wednesday | 4 | 240.30 | 59.49 | 4.0391 | high | down | single_break_down | down | 1.00 | — | 17 |
| new_york | 2026-07-29T08:00:00-04:00 | wednesday | 8 | 240.30 | 59.49 | 4.0391 | high | down | double_break_down_first | down | 1.00 | 4.25 | 18 |
| new_york | 2026-07-29T08:00:00-04:00 | wednesday | 12 | 240.30 | 59.49 | 4.0391 | high | down | double_break_down_first | down | 1.00 | 4.25 | 18 |
| new_york | 2026-07-29T08:00:00-04:00 | wednesday | 24 | 240.30 | 59.49 | 4.0391 | high | down | double_break_down_first | down | 1.00 | 4.25 | 18 |
| new_york | 2026-07-29T08:00:00-04:00 | wednesday | 48 | 240.30 | 59.49 | 4.0391 | high | down | double_break_down_first | down | 1.00 | 4.25 | 18 |
| tokyo | 2026-07-30T09:00:00+09:00 | thursday | 4 | 216.30 | 91.05 | 2.3756 | mid | down | double_break_up_first | up | 0.25 | 2.00 | 9 |
| tokyo | 2026-07-30T09:00:00+09:00 | thursday | 8 | 216.30 | 91.05 | 2.3756 | mid | down | double_break_up_first | up | 0.25 | 2.00 | 9 |
| tokyo | 2026-07-30T09:00:00+09:00 | thursday | 12 | 216.30 | 91.05 | 2.3756 | mid | down | double_break_up_first | up | 0.25 | 2.00 | 9 |
| tokyo | 2026-07-30T09:00:00+09:00 | thursday | 24 | 216.30 | 91.05 | 2.3756 | mid | down | double_break_up_first | up | 0.25 | 2.00 | 9 |
| tokyo | 2026-07-30T09:00:00+09:00 | thursday | 48 | 216.30 | 91.05 | 2.3756 | mid | down | double_break_up_first | up | 0.25 | 2.00 | 9 |
| london | 2026-07-30T08:00:00+01:00 | thursday | 4 | 157.00 | 93.23 | 1.6840 | low | up | single_break_up | up | 0.25 | — | 17 |
| london | 2026-07-30T08:00:00+01:00 | thursday | 8 | 157.00 | 93.23 | 1.6840 | low | up | single_break_up | up | 0.25 | — | 33 |
| london | 2026-07-30T08:00:00+01:00 | thursday | 12 | 157.00 | 93.23 | 1.6840 | low | up | single_break_up | up | 0.25 | — | 49 |
| london | 2026-07-30T08:00:00+01:00 | thursday | 24 | 157.00 | 93.23 | 1.6840 | low | up | single_break_up | up | 0.25 | — | 93 |
| london | 2026-07-30T08:00:00+01:00 | thursday | 48 | 157.00 | 93.23 | 1.6840 | low | up | double_break_up_first | up | 0.25 | 29.25 | 114 |
| new_york | 2026-07-30T08:00:00-04:00 | thursday | 4 | 193.70 | 82.96 | 2.3347 | mid | down | double_break_down_first | down | 0.25 | 0.75 | 4 |
| new_york | 2026-07-30T08:00:00-04:00 | thursday | 8 | 193.70 | 82.96 | 2.3347 | mid | down | double_break_down_first | down | 0.25 | 0.75 | 4 |
| new_york | 2026-07-30T08:00:00-04:00 | thursday | 12 | 193.70 | 82.96 | 2.3347 | mid | down | double_break_down_first | down | 0.25 | 0.75 | 4 |
| new_york | 2026-07-30T08:00:00-04:00 | thursday | 24 | 193.70 | 82.96 | 2.3347 | mid | down | double_break_down_first | down | 0.25 | 0.75 | 4 |
| new_york | 2026-07-30T08:00:00-04:00 | thursday | 48 | 193.70 | 82.96 | 2.3347 | mid | down | double_break_down_first | down | 0.25 | 0.75 | 4 |
| tokyo | 2026-07-31T09:00:00+09:00 | friday | 4 | 164.40 | 51.37 | 3.2002 | high | down | single_break_down | down | 0.25 | — | 17 |
| tokyo | 2026-07-31T09:00:00+09:00 | friday | 8 | 164.40 | 51.37 | 3.2002 | high | down | single_break_down | down | 0.25 | — | 33 |
| tokyo | 2026-07-31T09:00:00+09:00 | friday | 12 | 164.40 | 51.37 | 3.2002 | high | down | single_break_down | down | 0.25 | — | 49 |
| tokyo | 2026-07-31T09:00:00+09:00 | friday | 24 | 164.40 | 51.37 | 3.2002 | high | down | single_break_down | down | 0.25 | — | 81 |
| tokyo | 2026-07-31T09:00:00+09:00 | friday | 48 | 164.40 | 51.37 | 3.2002 | high | down | single_break_down | down | 0.25 | — | 81 |
| london | 2026-07-31T08:00:00+01:00 | friday | 4 | 115.10 | 53.61 | 2.1471 | low | down | single_break_down | down | 0.25 | — | 17 |
| london | 2026-07-31T08:00:00+01:00 | friday | 8 | 115.10 | 53.61 | 2.1471 | low | down | single_break_down | down | 0.25 | — | 33 |
| london | 2026-07-31T08:00:00+01:00 | friday | 12 | 115.10 | 53.61 | 2.1471 | low | down | single_break_down | down | 0.25 | — | 49 |
| london | 2026-07-31T08:00:00+01:00 | friday | 24 | 115.10 | 53.61 | 2.1471 | low | down | single_break_down | down | 0.25 | — | 53 |
| london | 2026-07-31T08:00:00+01:00 | friday | 48 | 115.10 | 53.61 | 2.1471 | low | down | single_break_down | down | 0.25 | — | 53 |
| new_york | 2026-07-31T08:00:00-04:00 | friday | 4 | 186.80 | 60.72 | 3.0763 | high | down | single_break_down | down | 0.25 | — | 17 |
| new_york | 2026-07-31T08:00:00-04:00 | friday | 8 | 186.80 | 60.72 | 3.0763 | high | down | single_break_down | down | 0.25 | — | 33 |
| new_york | 2026-07-31T08:00:00-04:00 | friday | 12 | 186.80 | 60.72 | 3.0763 | high | down | single_break_down | down | 0.25 | — | 33 |
| new_york | 2026-07-31T08:00:00-04:00 | friday | 24 | 186.80 | 60.72 | 3.0763 | high | down | single_break_down | down | 0.25 | — | 33 |
| new_york | 2026-07-31T08:00:00-04:00 | friday | 48 | 186.80 | 60.72 | 3.0763 | high | down | single_break_down | down | 0.25 | — | 33 |
| tokyo | 2026-08-03T09:00:00+09:00 | monday | 4 | 232.70 | 93.72 | 2.4829 | mid | down | single_break_down | down | 0.25 | — | 17 |
| tokyo | 2026-08-03T09:00:00+09:00 | monday | 8 | 232.70 | 93.72 | 2.4829 | mid | down | single_break_down | down | 0.25 | — | 33 |
| tokyo | 2026-08-03T09:00:00+09:00 | monday | 12 | 232.70 | 93.72 | 2.4829 | mid | down | single_break_down | down | 0.25 | — | 49 |
| tokyo | 2026-08-03T09:00:00+09:00 | monday | 24 | 232.70 | 93.72 | 2.4829 | mid | down | single_break_down | down | 0.25 | — | 93 |
| tokyo | 2026-08-03T09:00:00+09:00 | monday | 48 | 232.70 | 93.72 | 2.4829 | mid | down | double_break_down_first | down | 0.25 | 35.00 | 137 |
| london | 2026-08-03T08:00:00+01:00 | monday | 4 | 129.20 | 69.38 | 1.8622 | low | down | single_break_down | down | 1.50 | — | 17 |
| london | 2026-08-03T08:00:00+01:00 | monday | 8 | 129.20 | 69.38 | 1.8622 | low | down | single_break_down | down | 1.50 | — | 33 |
| london | 2026-08-03T08:00:00+01:00 | monday | 12 | 129.20 | 69.38 | 1.8622 | low | down | single_break_down | down | 1.50 | — | 49 |
| london | 2026-08-03T08:00:00+01:00 | monday | 24 | 129.20 | 69.38 | 1.8622 | low | down | double_break_down_first | down | 1.50 | 16.25 | 62 |
| london | 2026-08-03T08:00:00+01:00 | monday | 48 | 129.20 | 69.38 | 1.8622 | low | down | double_break_down_first | down | 1.50 | 16.25 | 62 |
| new_york | 2026-08-03T08:00:00-04:00 | monday | 4 | 222.60 | 65.57 | 3.3948 | high | down | single_break_down | down | 0.25 | — | 17 |
| new_york | 2026-08-03T08:00:00-04:00 | monday | 8 | 222.60 | 65.57 | 3.3948 | high | down | single_break_down | down | 0.25 | — | 33 |
| new_york | 2026-08-03T08:00:00-04:00 | monday | 12 | 222.60 | 65.57 | 3.3948 | high | down | double_break_down_first | down | 0.25 | 11.25 | 42 |
| new_york | 2026-08-03T08:00:00-04:00 | monday | 24 | 222.60 | 65.57 | 3.3948 | high | down | double_break_down_first | down | 0.25 | 11.25 | 42 |
| new_york | 2026-08-03T08:00:00-04:00 | monday | 48 | 222.60 | 65.57 | 3.3948 | high | down | double_break_down_first | down | 0.25 | 11.25 | 42 |
| tokyo | 2026-08-04T09:00:00+09:00 | tuesday | 4 | 205.80 | 48.94 | 4.2049 | high | down | single_break_down | down | 0.25 | — | 17 |
| tokyo | 2026-08-04T09:00:00+09:00 | tuesday | 8 | 205.80 | 48.94 | 4.2049 | high | down | double_break_down_first | down | 0.25 | 7.25 | 30 |
| tokyo | 2026-08-04T09:00:00+09:00 | tuesday | 12 | 205.80 | 48.94 | 4.2049 | high | down | double_break_down_first | down | 0.25 | 7.25 | 30 |
| tokyo | 2026-08-04T09:00:00+09:00 | tuesday | 24 | 205.80 | 48.94 | 4.2049 | high | down | double_break_down_first | down | 0.25 | 7.25 | 30 |
| tokyo | 2026-08-04T09:00:00+09:00 | tuesday | 48 | 205.80 | 48.94 | 4.2049 | high | down | double_break_down_first | down | 0.25 | 7.25 | 30 |
| london | 2026-08-04T08:00:00+01:00 | tuesday | 4 | 58.10 | 51.97 | 1.1179 | low | up | double_break_up_first | up | 0.25 | 0.50 | 3 |
| london | 2026-08-04T08:00:00+01:00 | tuesday | 8 | 58.10 | 51.97 | 1.1179 | low | up | double_break_up_first | up | 0.25 | 0.50 | 3 |
| london | 2026-08-04T08:00:00+01:00 | tuesday | 12 | 58.10 | 51.97 | 1.1179 | low | up | double_break_up_first | up | 0.25 | 0.50 | 3 |
| london | 2026-08-04T08:00:00+01:00 | tuesday | 24 | 58.10 | 51.97 | 1.1179 | low | up | double_break_up_first | up | 0.25 | 0.50 | 3 |
| london | 2026-08-04T08:00:00+01:00 | tuesday | 48 | 58.10 | 51.97 | 1.1179 | low | up | double_break_up_first | up | 0.25 | 0.50 | 3 |
| new_york | 2026-08-04T08:00:00-04:00 | tuesday | 4 | 233.50 | 86.36 | 2.7037 | mid | down | single_break_up | up | 0.50 | — | 17 |
| new_york | 2026-08-04T08:00:00-04:00 | tuesday | 8 | 233.50 | 86.36 | 2.7037 | mid | down | single_break_up | up | 0.50 | — | 33 |
| new_york | 2026-08-04T08:00:00-04:00 | tuesday | 12 | 233.50 | 86.36 | 2.7037 | mid | down | single_break_up | up | 0.50 | — | 45 |
| new_york | 2026-08-04T08:00:00-04:00 | tuesday | 24 | 233.50 | 86.36 | 2.7037 | mid | down | single_break_up | up | 0.50 | — | 93 |
| new_york | 2026-08-04T08:00:00-04:00 | tuesday | 48 | 233.50 | 86.36 | 2.7037 | mid | down | single_break_up | up | 0.50 | — | 185 |
| tokyo | 2026-08-05T09:00:00+09:00 | wednesday | 4 | 130.80 | 53.41 | 2.4488 | mid | up | single_break_up | up | 0.25 | — | 17 |
| tokyo | 2026-08-05T09:00:00+09:00 | wednesday | 8 | 130.80 | 53.41 | 2.4488 | mid | up | single_break_up | up | 0.25 | — | 33 |
| tokyo | 2026-08-05T09:00:00+09:00 | wednesday | 12 | 130.80 | 53.41 | 2.4488 | mid | up | single_break_up | up | 0.25 | — | 49 |
| tokyo | 2026-08-05T09:00:00+09:00 | wednesday | 24 | 130.80 | 53.41 | 2.4488 | mid | up | single_break_up | up | 0.25 | — | 93 |
| tokyo | 2026-08-05T09:00:00+09:00 | wednesday | 48 | 130.80 | 53.41 | 2.4488 | mid | up | single_break_up | up | 0.25 | — | 185 |
| london | 2026-08-05T08:00:00+01:00 | wednesday | 4 | 171.60 | 91.96 | 1.8661 | low | down | double_break_down_first | down | 0.50 | 3.50 | 15 |
| london | 2026-08-05T08:00:00+01:00 | wednesday | 8 | 171.60 | 91.96 | 1.8661 | low | down | double_break_down_first | down | 0.50 | 3.50 | 15 |
| london | 2026-08-05T08:00:00+01:00 | wednesday | 12 | 171.60 | 91.96 | 1.8661 | low | down | double_break_down_first | down | 0.50 | 3.50 | 15 |
| london | 2026-08-05T08:00:00+01:00 | wednesday | 24 | 171.60 | 91.96 | 1.8661 | low | down | double_break_down_first | down | 0.50 | 3.50 | 15 |
| london | 2026-08-05T08:00:00+01:00 | wednesday | 48 | 171.60 | 91.96 | 1.8661 | low | down | double_break_down_first | down | 0.50 | 3.50 | 15 |
| new_york | 2026-08-05T08:00:00-04:00 | wednesday | 4 | 301.20 | 98.95 | 3.0440 | high | down | single_break_up | up | 1.25 | — | 17 |
| new_york | 2026-08-05T08:00:00-04:00 | wednesday | 8 | 301.20 | 98.95 | 3.0440 | high | down | single_break_up | up | 1.25 | — | 33 |
| new_york | 2026-08-05T08:00:00-04:00 | wednesday | 12 | 301.20 | 98.95 | 3.0440 | high | down | single_break_up | up | 1.25 | — | 45 |
| new_york | 2026-08-05T08:00:00-04:00 | wednesday | 24 | 301.20 | 98.95 | 3.0440 | high | down | single_break_up | up | 1.25 | — | 93 |
| new_york | 2026-08-05T08:00:00-04:00 | wednesday | 48 | 301.20 | 98.95 | 3.0440 | high | down | single_break_up | up | 1.25 | — | 185 |
| tokyo | 2026-08-06T09:00:00+09:00 | thursday | 4 | 357.30 | 91.78 | 3.8931 | high | up | double_break_up_first | up | 0.25 | 2.50 | 11 |
| tokyo | 2026-08-06T09:00:00+09:00 | thursday | 8 | 357.30 | 91.78 | 3.8931 | high | up | double_break_up_first | up | 0.25 | 2.50 | 11 |
| tokyo | 2026-08-06T09:00:00+09:00 | thursday | 12 | 357.30 | 91.78 | 3.8931 | high | up | double_break_up_first | up | 0.25 | 2.50 | 11 |
| tokyo | 2026-08-06T09:00:00+09:00 | thursday | 24 | 357.30 | 91.78 | 3.8931 | high | up | double_break_up_first | up | 0.25 | 2.50 | 11 |
| tokyo | 2026-08-06T09:00:00+09:00 | thursday | 48 | 357.30 | 91.78 | 3.8931 | high | up | double_break_up_first | up | 0.25 | 2.50 | 11 |
| london | 2026-08-06T08:00:00+01:00 | thursday | 4 | 125.70 | 85.21 | 1.4752 | low | up | single_break_up | up | 0.25 | — | 17 |
| london | 2026-08-06T08:00:00+01:00 | thursday | 8 | 125.70 | 85.21 | 1.4752 | low | up | double_break_up_first | up | 0.25 | 5.00 | 21 |
| london | 2026-08-06T08:00:00+01:00 | thursday | 12 | 125.70 | 85.21 | 1.4752 | low | up | double_break_up_first | up | 0.25 | 5.00 | 21 |
| london | 2026-08-06T08:00:00+01:00 | thursday | 24 | 125.70 | 85.21 | 1.4752 | low | up | double_break_up_first | up | 0.25 | 5.00 | 21 |
| london | 2026-08-06T08:00:00+01:00 | thursday | 48 | 125.70 | 85.21 | 1.4752 | low | up | double_break_up_first | up | 0.25 | 5.00 | 21 |
| new_york | 2026-08-06T08:00:00-04:00 | thursday | 4 | 213.80 | 86.91 | 2.4599 | mid | down | double_break_down_first | down | 0.25 | 0.75 | 4 |
| new_york | 2026-08-06T08:00:00-04:00 | thursday | 8 | 213.80 | 86.91 | 2.4599 | mid | down | double_break_down_first | down | 0.25 | 0.75 | 4 |
| new_york | 2026-08-06T08:00:00-04:00 | thursday | 12 | 213.80 | 86.91 | 2.4599 | mid | down | double_break_down_first | down | 0.25 | 0.75 | 4 |
| new_york | 2026-08-06T08:00:00-04:00 | thursday | 24 | 213.80 | 86.91 | 2.4599 | mid | down | double_break_down_first | down | 0.25 | 0.75 | 4 |
| new_york | 2026-08-06T08:00:00-04:00 | thursday | 48 | 213.80 | 86.91 | 2.4599 | mid | down | double_break_down_first | down | 0.25 | 0.75 | 4 |
| tokyo | 2026-08-07T09:00:00+09:00 | friday | 4 | 228.40 | 63.36 | 3.6046 | high | down | single_break_up | up | 0.75 | — | 17 |
| tokyo | 2026-08-07T09:00:00+09:00 | friday | 8 | 228.40 | 63.36 | 3.6046 | high | down | single_break_up | up | 0.75 | — | 33 |
| tokyo | 2026-08-07T09:00:00+09:00 | friday | 12 | 228.40 | 63.36 | 3.6046 | high | down | single_break_up | up | 0.75 | — | 49 |
| tokyo | 2026-08-07T09:00:00+09:00 | friday | 24 | 228.40 | 63.36 | 3.6046 | high | down | single_break_up | up | 0.75 | — | 81 |
| tokyo | 2026-08-07T09:00:00+09:00 | friday | 48 | 228.40 | 63.36 | 3.6046 | high | down | single_break_up | up | 0.75 | — | 81 |
| london | 2026-08-07T08:00:00+01:00 | friday | 4 | 149.20 | 87.16 | 1.7119 | low | down | single_break_up | up | 0.25 | — | 17 |
| london | 2026-08-07T08:00:00+01:00 | friday | 8 | 149.20 | 87.16 | 1.7119 | low | down | single_break_up | up | 0.25 | — | 33 |
| london | 2026-08-07T08:00:00+01:00 | friday | 12 | 149.20 | 87.16 | 1.7119 | low | down | single_break_up | up | 0.25 | — | 49 |
| london | 2026-08-07T08:00:00+01:00 | friday | 24 | 149.20 | 87.16 | 1.7119 | low | down | single_break_up | up | 0.25 | — | 53 |
| london | 2026-08-07T08:00:00+01:00 | friday | 48 | 149.20 | 87.16 | 1.7119 | low | down | single_break_up | up | 0.25 | — | 53 |
| new_york | 2026-08-07T08:00:00-04:00 | friday | 4 | 696.70 | 130.78 | 5.3273 | high | up | no_break | none | — | — | 17 |
| new_york | 2026-08-07T08:00:00-04:00 | friday | 8 | 696.70 | 130.78 | 5.3273 | high | up | no_break | none | — | — | 33 |
| new_york | 2026-08-07T08:00:00-04:00 | friday | 12 | 696.70 | 130.78 | 5.3273 | high | up | no_break | none | — | — | 33 |
| new_york | 2026-08-07T08:00:00-04:00 | friday | 24 | 696.70 | 130.78 | 5.3273 | high | up | no_break | none | — | — | 33 |
| new_york | 2026-08-07T08:00:00-04:00 | friday | 48 | 696.70 | 130.78 | 5.3273 | high | up | no_break | none | — | — | 33 |
| tokyo | 2026-08-10T09:00:00+09:00 | monday | 4 | 163.10 | 72.58 | 2.2472 | mid | down | single_break_down | down | 0.25 | — | 17 |
| tokyo | 2026-08-10T09:00:00+09:00 | monday | 8 | 163.10 | 72.58 | 2.2472 | mid | down | double_break_down_first | down | 0.25 | 5.00 | 21 |
| tokyo | 2026-08-10T09:00:00+09:00 | monday | 12 | 163.10 | 72.58 | 2.2472 | mid | down | double_break_down_first | down | 0.25 | 5.00 | 21 |
| tokyo | 2026-08-10T09:00:00+09:00 | monday | 24 | 163.10 | 72.58 | 2.2472 | mid | down | double_break_down_first | down | 0.25 | 5.00 | 21 |
| tokyo | 2026-08-10T09:00:00+09:00 | monday | 48 | 163.10 | 72.58 | 2.2472 | mid | down | double_break_down_first | down | 0.25 | 5.00 | 21 |
| london | 2026-08-10T08:00:00+01:00 | monday | 4 | 118.30 | 77.65 | 1.5235 | low | down | single_break_down | down | 0.75 | — | 17 |
| london | 2026-08-10T08:00:00+01:00 | monday | 8 | 118.30 | 77.65 | 1.5235 | low | down | single_break_down | down | 0.75 | — | 33 |
| london | 2026-08-10T08:00:00+01:00 | monday | 12 | 118.30 | 77.65 | 1.5235 | low | down | double_break_down_first | down | 0.75 | 8.25 | 34 |
| london | 2026-08-10T08:00:00+01:00 | monday | 24 | 118.30 | 77.65 | 1.5235 | low | down | double_break_down_first | down | 0.75 | 8.25 | 34 |
| london | 2026-08-10T08:00:00+01:00 | monday | 48 | 118.30 | 77.65 | 1.5235 | low | down | double_break_down_first | down | 0.75 | 8.25 | 34 |
| new_york | 2026-08-10T08:00:00-04:00 | monday | 4 | 128.70 | 57.97 | 2.2201 | mid | down | double_break_up_first | up | 0.25 | 1.00 | 5 |
| new_york | 2026-08-10T08:00:00-04:00 | monday | 8 | 128.70 | 57.97 | 2.2201 | mid | down | double_break_up_first | up | 0.25 | 1.00 | 5 |
| new_york | 2026-08-10T08:00:00-04:00 | monday | 12 | 128.70 | 57.97 | 2.2201 | mid | down | double_break_up_first | up | 0.25 | 1.00 | 5 |
| new_york | 2026-08-10T08:00:00-04:00 | monday | 24 | 128.70 | 57.97 | 2.2201 | mid | down | double_break_up_first | up | 0.25 | 1.00 | 5 |
| new_york | 2026-08-10T08:00:00-04:00 | monday | 48 | 128.70 | 57.97 | 2.2201 | mid | down | double_break_up_first | up | 0.25 | 1.00 | 5 |
| tokyo | 2026-08-11T09:00:00+09:00 | tuesday | 4 | 227.10 | 57.30 | 3.9634 | high | up | double_break_down_first | down | 0.25 | 0.50 | 3 |
| tokyo | 2026-08-11T09:00:00+09:00 | tuesday | 8 | 227.10 | 57.30 | 3.9634 | high | up | double_break_down_first | down | 0.25 | 0.50 | 3 |
| tokyo | 2026-08-11T09:00:00+09:00 | tuesday | 12 | 227.10 | 57.30 | 3.9634 | high | up | double_break_down_first | down | 0.25 | 0.50 | 3 |
| tokyo | 2026-08-11T09:00:00+09:00 | tuesday | 24 | 227.10 | 57.30 | 3.9634 | high | up | double_break_down_first | down | 0.25 | 0.50 | 3 |
| tokyo | 2026-08-11T09:00:00+09:00 | tuesday | 48 | 227.10 | 57.30 | 3.9634 | high | up | double_break_down_first | down | 0.25 | 0.50 | 3 |
| london | 2026-08-11T08:00:00+01:00 | tuesday | 4 | 109.20 | 99.73 | 1.0950 | low | down | single_break_up | up | 0.50 | — | 17 |
| london | 2026-08-11T08:00:00+01:00 | tuesday | 8 | 109.20 | 99.73 | 1.0950 | low | down | single_break_up | up | 0.50 | — | 33 |
| london | 2026-08-11T08:00:00+01:00 | tuesday | 12 | 109.20 | 99.73 | 1.0950 | low | down | single_break_up | up | 0.50 | — | 49 |
| london | 2026-08-11T08:00:00+01:00 | tuesday | 24 | 109.20 | 99.73 | 1.0950 | low | down | single_break_up | up | 0.50 | — | 93 |
| london | 2026-08-11T08:00:00+01:00 | tuesday | 48 | 109.20 | 99.73 | 1.0950 | low | down | single_break_up | up | 0.50 | — | 185 |
| new_york | 2026-08-11T08:00:00-04:00 | tuesday | 4 | 160.50 | 74.72 | 2.1480 | mid | up | double_break_up_first | up | 0.25 | 1.25 | 6 |
| new_york | 2026-08-11T08:00:00-04:00 | tuesday | 8 | 160.50 | 74.72 | 2.1480 | mid | up | double_break_up_first | up | 0.25 | 1.25 | 6 |
| new_york | 2026-08-11T08:00:00-04:00 | tuesday | 12 | 160.50 | 74.72 | 2.1480 | mid | up | double_break_up_first | up | 0.25 | 1.25 | 6 |
| new_york | 2026-08-11T08:00:00-04:00 | tuesday | 24 | 160.50 | 74.72 | 2.1480 | mid | up | double_break_up_first | up | 0.25 | 1.25 | 6 |
| new_york | 2026-08-11T08:00:00-04:00 | tuesday | 48 | 160.50 | 74.72 | 2.1480 | mid | up | double_break_up_first | up | 0.25 | 1.25 | 6 |
| tokyo | 2026-08-12T09:00:00+09:00 | wednesday | 4 | 172.10 | 61.59 | 2.7942 | mid | up | single_break_up | up | 0.25 | — | 17 |
| tokyo | 2026-08-12T09:00:00+09:00 | wednesday | 8 | 172.10 | 61.59 | 2.7942 | mid | up | single_break_up | up | 0.25 | — | 33 |
| tokyo | 2026-08-12T09:00:00+09:00 | wednesday | 12 | 172.10 | 61.59 | 2.7942 | mid | up | single_break_up | up | 0.25 | — | 49 |
| tokyo | 2026-08-12T09:00:00+09:00 | wednesday | 24 | 172.10 | 61.59 | 2.7942 | mid | up | single_break_up | up | 0.25 | — | 93 |
| tokyo | 2026-08-12T09:00:00+09:00 | wednesday | 48 | 172.10 | 61.59 | 2.7942 | mid | up | double_break_up_first | up | 0.25 | 31.25 | 122 |
| london | 2026-08-12T08:00:00+01:00 | wednesday | 4 | 118.00 | 67.86 | 1.7389 | low | up | single_break_up | up | 0.25 | — | 17 |
| london | 2026-08-12T08:00:00+01:00 | wednesday | 8 | 118.00 | 67.86 | 1.7389 | low | up | double_break_up_first | up | 0.25 | 4.75 | 20 |
| london | 2026-08-12T08:00:00+01:00 | wednesday | 12 | 118.00 | 67.86 | 1.7389 | low | up | double_break_up_first | up | 0.25 | 4.75 | 20 |
| london | 2026-08-12T08:00:00+01:00 | wednesday | 24 | 118.00 | 67.86 | 1.7389 | low | up | double_break_up_first | up | 0.25 | 4.75 | 20 |
| london | 2026-08-12T08:00:00+01:00 | wednesday | 48 | 118.00 | 67.86 | 1.7389 | low | up | double_break_up_first | up | 0.25 | 4.75 | 20 |
| new_york | 2026-08-12T08:00:00-04:00 | wednesday | 4 | 557.00 | 118.09 | 4.7169 | high | up | single_break_up | up | 0.25 | — | 17 |
| new_york | 2026-08-12T08:00:00-04:00 | wednesday | 8 | 557.00 | 118.09 | 4.7169 | high | up | single_break_up | up | 0.25 | — | 33 |
| new_york | 2026-08-12T08:00:00-04:00 | wednesday | 12 | 557.00 | 118.09 | 4.7169 | high | up | single_break_up | up | 0.25 | — | 45 |
| new_york | 2026-08-12T08:00:00-04:00 | wednesday | 24 | 557.00 | 118.09 | 4.7169 | high | up | double_break_up_first | up | 0.25 | 17.00 | 65 |
| new_york | 2026-08-12T08:00:00-04:00 | wednesday | 48 | 557.00 | 118.09 | 4.7169 | high | up | double_break_up_first | up | 0.25 | 17.00 | 65 |
| tokyo | 2026-08-13T09:00:00+09:00 | thursday | 4 | 368.60 | 77.59 | 4.7504 | high | up | single_break_down | down | 1.00 | — | 17 |
| tokyo | 2026-08-13T09:00:00+09:00 | thursday | 8 | 368.60 | 77.59 | 4.7504 | high | up | single_break_down | down | 1.00 | — | 33 |
| tokyo | 2026-08-13T09:00:00+09:00 | thursday | 12 | 368.60 | 77.59 | 4.7504 | high | up | single_break_down | down | 1.00 | — | 49 |
| tokyo | 2026-08-13T09:00:00+09:00 | thursday | 24 | 368.60 | 77.59 | 4.7504 | high | up | single_break_down | down | 1.00 | — | 93 |
| tokyo | 2026-08-13T09:00:00+09:00 | thursday | 48 | 368.60 | 77.59 | 4.7504 | high | up | single_break_down | down | 1.00 | — | 173 |
| london | 2026-08-13T08:00:00+01:00 | thursday | 4 | 123.20 | 80.42 | 1.5319 | low | down | double_break_down_first | down | 0.25 | 1.50 | 7 |
| london | 2026-08-13T08:00:00+01:00 | thursday | 8 | 123.20 | 80.42 | 1.5319 | low | down | double_break_down_first | down | 0.25 | 1.50 | 7 |
| london | 2026-08-13T08:00:00+01:00 | thursday | 12 | 123.20 | 80.42 | 1.5319 | low | down | double_break_down_first | down | 0.25 | 1.50 | 7 |
| london | 2026-08-13T08:00:00+01:00 | thursday | 24 | 123.20 | 80.42 | 1.5319 | low | down | double_break_down_first | down | 0.25 | 1.50 | 7 |
| london | 2026-08-13T08:00:00+01:00 | thursday | 48 | 123.20 | 80.42 | 1.5319 | low | down | double_break_down_first | down | 0.25 | 1.50 | 7 |
| new_york | 2026-08-13T08:00:00-04:00 | thursday | 4 | 209.20 | 71.70 | 2.9177 | mid | up | double_break_up_first | up | 0.25 | 0.75 | 4 |
| new_york | 2026-08-13T08:00:00-04:00 | thursday | 8 | 209.20 | 71.70 | 2.9177 | mid | up | double_break_up_first | up | 0.25 | 0.75 | 4 |
| new_york | 2026-08-13T08:00:00-04:00 | thursday | 12 | 209.20 | 71.70 | 2.9177 | mid | up | double_break_up_first | up | 0.25 | 0.75 | 4 |
| new_york | 2026-08-13T08:00:00-04:00 | thursday | 24 | 209.20 | 71.70 | 2.9177 | mid | up | double_break_up_first | up | 0.25 | 0.75 | 4 |
| new_york | 2026-08-13T08:00:00-04:00 | thursday | 48 | 209.20 | 71.70 | 2.9177 | mid | up | double_break_up_first | up | 0.25 | 0.75 | 4 |
| tokyo | 2026-08-14T09:00:00+09:00 | friday | 4 | 324.30 | 79.74 | 4.0668 | high | down | single_break_down | down | 0.25 | — | 17 |
| tokyo | 2026-08-14T09:00:00+09:00 | friday | 8 | 324.30 | 79.74 | 4.0668 | high | down | single_break_down | down | 0.25 | — | 33 |
| tokyo | 2026-08-14T09:00:00+09:00 | friday | 12 | 324.30 | 79.74 | 4.0668 | high | down | double_break_down_first | down | 0.25 | 10.75 | 44 |
| tokyo | 2026-08-14T09:00:00+09:00 | friday | 24 | 324.30 | 79.74 | 4.0668 | high | down | double_break_down_first | down | 0.25 | 10.75 | 44 |
| tokyo | 2026-08-14T09:00:00+09:00 | friday | 48 | 324.30 | 79.74 | 4.0668 | high | down | double_break_down_first | down | 0.25 | 10.75 | 44 |
| london | 2026-08-14T08:00:00+01:00 | friday | 4 | 154.90 | 66.86 | 2.3169 | mid | up | single_break_up | up | 0.25 | — | 17 |
| london | 2026-08-14T08:00:00+01:00 | friday | 8 | 154.90 | 66.86 | 2.3169 | mid | up | single_break_up | up | 0.25 | — | 33 |
| london | 2026-08-14T08:00:00+01:00 | friday | 12 | 154.90 | 66.86 | 2.3169 | mid | up | single_break_up | up | 0.25 | — | 49 |
| london | 2026-08-14T08:00:00+01:00 | friday | 24 | 154.90 | 66.86 | 2.3169 | mid | up | single_break_up | up | 0.25 | — | 53 |
| london | 2026-08-14T08:00:00+01:00 | friday | 48 | 154.90 | 66.86 | 2.3169 | mid | up | single_break_up | up | 0.25 | — | 53 |
| new_york | 2026-08-14T08:00:00-04:00 | friday | 4 | 202.60 | 67.67 | 2.9939 | mid | up | single_break_up | up | 0.75 | — | 17 |
| new_york | 2026-08-14T08:00:00-04:00 | friday | 8 | 202.60 | 67.67 | 2.9939 | mid | up | single_break_up | up | 0.75 | — | 33 |
| new_york | 2026-08-14T08:00:00-04:00 | friday | 12 | 202.60 | 67.67 | 2.9939 | mid | up | single_break_up | up | 0.75 | — | 33 |
| new_york | 2026-08-14T08:00:00-04:00 | friday | 24 | 202.60 | 67.67 | 2.9939 | mid | up | single_break_up | up | 0.75 | — | 33 |
| new_york | 2026-08-14T08:00:00-04:00 | friday | 48 | 202.60 | 67.67 | 2.9939 | mid | up | single_break_up | up | 0.75 | — | 33 |
| tokyo | 2026-08-17T09:00:00+09:00 | monday | 4 | 340.20 | 69.84 | 4.8709 | high | up | single_break_up | up | 0.25 | — | 17 |
| tokyo | 2026-08-17T09:00:00+09:00 | monday | 8 | 340.20 | 69.84 | 4.8709 | high | up | single_break_up | up | 0.25 | — | 33 |
| tokyo | 2026-08-17T09:00:00+09:00 | monday | 12 | 340.20 | 69.84 | 4.8709 | high | up | single_break_up | up | 0.25 | — | 49 |
| tokyo | 2026-08-17T09:00:00+09:00 | monday | 24 | 340.20 | 69.84 | 4.8709 | high | up | single_break_up | up | 0.25 | — | 93 |
| tokyo | 2026-08-17T09:00:00+09:00 | monday | 48 | 340.20 | 69.84 | 4.8709 | high | up | double_break_up_first | up | 0.25 | 38.00 | 149 |
| london | 2026-08-17T08:00:00+01:00 | monday | 4 | 152.70 | 74.34 | 2.0540 | low | up | single_break_down | down | 0.75 | — | 17 |
| london | 2026-08-17T08:00:00+01:00 | monday | 8 | 152.70 | 74.34 | 2.0540 | low | up | double_break_down_first | down | 0.75 | 6.00 | 25 |
| london | 2026-08-17T08:00:00+01:00 | monday | 12 | 152.70 | 74.34 | 2.0540 | low | up | double_break_down_first | down | 0.75 | 6.00 | 25 |
| london | 2026-08-17T08:00:00+01:00 | monday | 24 | 152.70 | 74.34 | 2.0540 | low | up | double_break_down_first | down | 0.75 | 6.00 | 25 |
| london | 2026-08-17T08:00:00+01:00 | monday | 48 | 152.70 | 74.34 | 2.0540 | low | up | double_break_down_first | down | 0.75 | 6.00 | 25 |
| new_york | 2026-08-17T08:00:00-04:00 | monday | 4 | 213.80 | 74.59 | 2.8662 | mid | down | single_break_up | up | 1.00 | — | 17 |
| new_york | 2026-08-17T08:00:00-04:00 | monday | 8 | 213.80 | 74.59 | 2.8662 | mid | down | single_break_up | up | 1.00 | — | 33 |
| new_york | 2026-08-17T08:00:00-04:00 | monday | 12 | 213.80 | 74.59 | 2.8662 | mid | down | single_break_up | up | 1.00 | — | 45 |
| new_york | 2026-08-17T08:00:00-04:00 | monday | 24 | 213.80 | 74.59 | 2.8662 | mid | down | single_break_up | up | 1.00 | — | 93 |
| new_york | 2026-08-17T08:00:00-04:00 | monday | 48 | 213.80 | 74.59 | 2.8662 | mid | down | double_break_up_first | up | 1.00 | 24.75 | 96 |
| tokyo | 2026-08-18T09:00:00+09:00 | tuesday | 4 | 159.50 | 51.37 | 3.1048 | high | up | double_break_up_first | up | 0.25 | 0.75 | 4 |
| tokyo | 2026-08-18T09:00:00+09:00 | tuesday | 8 | 159.50 | 51.37 | 3.1048 | high | up | double_break_up_first | up | 0.25 | 0.75 | 4 |
| tokyo | 2026-08-18T09:00:00+09:00 | tuesday | 12 | 159.50 | 51.37 | 3.1048 | high | up | double_break_up_first | up | 0.25 | 0.75 | 4 |
| tokyo | 2026-08-18T09:00:00+09:00 | tuesday | 24 | 159.50 | 51.37 | 3.1048 | high | up | double_break_up_first | up | 0.25 | 0.75 | 4 |
| tokyo | 2026-08-18T09:00:00+09:00 | tuesday | 48 | 159.50 | 51.37 | 3.1048 | high | up | double_break_up_first | up | 0.25 | 0.75 | 4 |
| london | 2026-08-18T08:00:00+01:00 | tuesday | 4 | 82.50 | 52.98 | 1.5572 | low | down | single_break_down | down | 0.25 | — | 17 |
| london | 2026-08-18T08:00:00+01:00 | tuesday | 8 | 82.50 | 52.98 | 1.5572 | low | down | double_break_down_first | down | 0.25 | 5.75 | 24 |
| london | 2026-08-18T08:00:00+01:00 | tuesday | 12 | 82.50 | 52.98 | 1.5572 | low | down | double_break_down_first | down | 0.25 | 5.75 | 24 |
| london | 2026-08-18T08:00:00+01:00 | tuesday | 24 | 82.50 | 52.98 | 1.5572 | low | down | double_break_down_first | down | 0.25 | 5.75 | 24 |
| london | 2026-08-18T08:00:00+01:00 | tuesday | 48 | 82.50 | 52.98 | 1.5572 | low | down | double_break_down_first | down | 0.25 | 5.75 | 24 |
| new_york | 2026-08-18T08:00:00-04:00 | tuesday | 4 | 107.60 | 62.41 | 1.7240 | low | down | double_break_down_first | down | 0.25 | 0.75 | 4 |
| new_york | 2026-08-18T08:00:00-04:00 | tuesday | 8 | 107.60 | 62.41 | 1.7240 | low | down | double_break_down_first | down | 0.25 | 0.75 | 4 |
| new_york | 2026-08-18T08:00:00-04:00 | tuesday | 12 | 107.60 | 62.41 | 1.7240 | low | down | double_break_down_first | down | 0.25 | 0.75 | 4 |
| new_york | 2026-08-18T08:00:00-04:00 | tuesday | 24 | 107.60 | 62.41 | 1.7240 | low | down | double_break_down_first | down | 0.25 | 0.75 | 4 |
| new_york | 2026-08-18T08:00:00-04:00 | tuesday | 48 | 107.60 | 62.41 | 1.7240 | low | down | double_break_down_first | down | 0.25 | 0.75 | 4 |
| tokyo | 2026-08-19T09:00:00+09:00 | wednesday | 4 | 156.30 | 69.48 | 2.2496 | mid | up | double_break_down_first | down | 0.25 | 0.75 | 4 |
| tokyo | 2026-08-19T09:00:00+09:00 | wednesday | 8 | 156.30 | 69.48 | 2.2496 | mid | up | double_break_down_first | down | 0.25 | 0.75 | 4 |
| tokyo | 2026-08-19T09:00:00+09:00 | wednesday | 12 | 156.30 | 69.48 | 2.2496 | mid | up | double_break_down_first | down | 0.25 | 0.75 | 4 |
| tokyo | 2026-08-19T09:00:00+09:00 | wednesday | 24 | 156.30 | 69.48 | 2.2496 | mid | up | double_break_down_first | down | 0.25 | 0.75 | 4 |
| tokyo | 2026-08-19T09:00:00+09:00 | wednesday | 48 | 156.30 | 69.48 | 2.2496 | mid | up | double_break_down_first | down | 0.25 | 0.75 | 4 |
| london | 2026-08-19T08:00:00+01:00 | wednesday | 4 | 156.30 | 79.51 | 1.9659 | low | up | single_break_up | up | 1.00 | — | 17 |
| london | 2026-08-19T08:00:00+01:00 | wednesday | 8 | 156.30 | 79.51 | 1.9659 | low | up | single_break_up | up | 1.00 | — | 33 |
| london | 2026-08-19T08:00:00+01:00 | wednesday | 12 | 156.30 | 79.51 | 1.9659 | low | up | single_break_up | up | 1.00 | — | 49 |
| london | 2026-08-19T08:00:00+01:00 | wednesday | 24 | 156.30 | 79.51 | 1.9659 | low | up | single_break_up | up | 1.00 | — | 59 |
| london | 2026-08-19T08:00:00+01:00 | wednesday | 48 | 156.30 | 79.51 | 1.9659 | low | up | single_break_up | up | 1.00 | — | 59 |
| new_york | 2026-08-19T08:00:00-04:00 | wednesday | 4 | 801.60 | 106.91 | 7.4981 | high | up | single_break_up | up | 0.25 | — | 17 |
| new_york | 2026-08-19T08:00:00-04:00 | wednesday | 8 | 801.60 | 106.91 | 7.4981 | high | up | single_break_up | up | 0.25 | — | 33 |
| new_york | 2026-08-19T08:00:00-04:00 | wednesday | 12 | 801.60 | 106.91 | 7.4981 | high | up | single_break_up | up | 0.25 | — | 39 |
| new_york | 2026-08-19T08:00:00-04:00 | wednesday | 24 | 801.60 | 106.91 | 7.4981 | high | up | single_break_up | up | 0.25 | — | 39 |
| new_york | 2026-08-19T08:00:00-04:00 | wednesday | 48 | 801.60 | 106.91 | 7.4981 | high | up | single_break_up | up | 0.25 | — | 39 |

## Caveats

- 65 episodes over 2000 M15 bars. Weekday and tercile subgroups hold roughly a dozen episodes each; their rates are indicative only, which is what the intervals say.
- M1 bars were present but covered only 93 of 2000 parent bars (4.65% of the window). Mixing M1 chronology on part of the window with the fallback on the rest would make results inside one study incomparable, so no M1 chronology was used: the whole window was resolved with the conservative pessimistic_same_bar_no_subpath fallback, in which a bar touching both the stop and the target is taken as the stop.
- A break is any trade beyond the range extreme by any amount. No buffer is applied, so these rates are the most generous possible reading of 'a side broke'.
- Episodes repeat across horizons by construction: the same session-day appears once per horizon, so rows are comparable within a horizon and must not be pooled across horizons.
- This study selects nothing. It measures how often the second side is tested; whether that is worth hedging against is a §9 question.
