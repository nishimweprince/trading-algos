# S5 resolver ladder bias calibration

One immutable configuration is run through every executable resolver tier. Tier 4 is reported as interface-only because no bid/ask tick source is implemented. All deltas are measured against tier 0 (`optimistic`).

## Run identity

| Field | Value |
|---|---|
| Symbol / timeframe | XAUUSD / M15 |
| Bars | 2000 |
| Date bounds | 2026-07-21T05:45:00+00:00 to 2026-08-19T23:30:00+00:00 |
| Candle fingerprint | `85ab375472c64e92519d07f91ba0e1e06ec3c713e8921e88f81fef3d22bda900` |
| M1 coverage | partial: 93 / 2000 (4.65%) |
| Uniform fallback | `pessimistic_same_bar_no_subpath` |

Partial M1 chronology is never mixed into a window. When coverage is not complete, tiers 2 and 3 use `pessimistic_same_bar_no_subpath` for the full window.

## Resolver totals and deltas

| Tier | Mode | Status | Fallback | Completed | Same-bar rate | Same-bar R | Gross pips | Net pips | Gross R | Net R | Δ gross pips | Δ net pips | Δ gross R | Δ net R | Changed structures |
|---|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 0 | optimistic | executed | — | 48.00 | 0.2292 | -1.6935 | 657.90 | 657.90 | 0.8680 | 0.8680 | 0.00 | 0.00 | 0.0000 | 0.0000 | 0.00 |
| 1 | pessimistic | executed | — | 48.00 | 0.2500 | -2.5723 | 727.70 | 727.70 | 1.2911 | 1.2911 | 69.80 | 69.80 | 0.4230 | 0.4230 | 1.00 |
| 2 | m1 | executed | pessimistic_same_bar_no_subpath | 48.00 | 0.2500 | -2.5723 | 727.70 | 727.70 | 1.2911 | 1.2911 | 69.80 | 69.80 | 0.4230 | 0.4230 | 1.00 |
| 3 | m1_conservative | executed | pessimistic_same_bar_no_subpath | 48.00 | 0.2500 | -2.5723 | 727.70 | 727.70 | 1.2911 | 1.2911 | 69.80 | 69.80 | 0.4230 | 0.4230 | 1.00 |
| 4 | tick | interface_only_unavailable | — | — | — | — | — | — | — | — | — | — | — | — | — |

## Every changed structure

A row appears when a structure is added, missing, changes classification, receives a different same-bar tag, or changes pip/R value versus tier 0. No changed structure is discarded.

| Tier | Structure | Change | Tier 0 outcome | Tier outcome | Tier 0 gross pips | Tier gross pips | Tier 0 gross R | Tier gross R |
|---|---|---|---|---|---:|---:|---:|---:|
| 1 | london:2026-08-18T08:00:00+00:00 | outcome_or_value_changed | whipsaw | lock | -214.80 | -145.00 | -1.3018 | -0.8788 |
| 2 | london:2026-08-18T08:00:00+00:00 | outcome_or_value_changed | whipsaw | lock | -214.80 | -145.00 | -1.3018 | -0.8788 |
| 3 | london:2026-08-18T08:00:00+00:00 | outcome_or_value_changed | whipsaw | lock | -214.80 | -145.00 | -1.3018 | -0.8788 |

## Calibration status and limits

The §0 same-bar comparison remains **unverified**: M15 10.6%, H1 11.2%, H4 5.1%. The named export CSVs are absent from `tests/fixtures`; no fixture data was invented, synthesized, or approximated. The export-dependent acceptance test remains an explicit skip.

The 2,000-bar M15 cache covers roughly 30 days of one symbol. It verifies this harness and provides descriptive calibration only; it cannot establish a resolver constant for other timeframes or regimes. That needs the named exports and contiguous M15 history with covering M1 or bid/ask ticks across varied regimes.
