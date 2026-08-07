Automatic meta-labeling is feasible with this codebase; you do not need to label 17 years manually. The current taggers already emit setup, state, confidence, and usually direction through `BarTag.side` in [types.py](/Users/nishimweprince/Documents/Markets/Apps/trading-algos/lookup-trader/server/app/taggers/types.py:74).

The main missing component is an event builder that converts those tags into sparse, directional trade candidates and automatically assigns outcomes.

## Important revisions to the proposed order

I agree with the raw-price diagnosis and keeping the 24-bar triple barrier. I would adjust three details:

1. Do not run the current trainer after rebuilding. Use `--skip-train`; it still only compares logistic regression and HistGradientBoosting.
2. Do not delete ATR from the dataset entirely. Remove it from estimator inputs, but retain it as label/execution metadata for barriers, costs, and expected-R calculations.
3. Uniqueness weighting will not materially change model fitting while every bar is an event. With dense fixed 24-bar labels, nearly every interior row receives approximately the same `1/24` weight. It correctly describes effective sample size, but constant weights do not change the fitted model. It becomes genuinely useful after sparse meta-events are introduced.

## Phase 0: Freeze the trade contract

Before the expensive rebuild, define one immutable initial strategy contract:

- Signal decision: after an H1 candle closes.
- Direction: supplied by the base tagger.
- Entry: next available H1 open or actual executable quote—not the signal candle’s close.
- Stop: 1 ATR measured at signal time.
- Target: 1.5 ATR.
- Maximum holding period: 24 H1 bars after entry.
- Ambiguous bar: loss/conservative.
- Exit at horizon: mark to market.
- Costs: pair-specific spread plus conservative slippage.
- Maximum: one event per `(symbol, timestamp, side)`.

The current labels and shadow worker anchor entry at the signal close. That is unsuitable for deployment because a signal calculated from the closed bar cannot reliably fill at that historical close. The stored `next_open` can be used for label construction while remaining categorically forbidden as a model feature.

Keep the 24-bar horizon for version 1. Test 12/24/48 as separate, predeclared contracts later. ATR-scaled horizontal barriers already provide substantial volatility normalization; changing the vertical barrier simultaneously would make it hard to identify what improved results.

## Phase 1: Repair data and direction semantics

Before rebuilding:

- Repair or quarantine the damaged 2023 segment.
- Treat abnormal market-open gaps as a failing quality gate.
- Keep raw price and ATR in the feature store—they are useful operationally—but prevent them from reaching the model.
- Drop `volume_z` from model inputs because all HistData volume values are zero.
- Fix neutral chart-pattern directions.

Of 32 seeded setups, approximately 23 currently have actual detectors. The Fibonacci and key-level families are mostly vocabulary without implementations, explaining many zero tag columns.

Several implemented patterns are also directionless in [setups_seed.py](/Users/nishimweprince/Documents/Markets/Apps/trading-algos/lookup-trader/server/app/db/setups_seed.py:10), including symmetrical triangles, rectangles, and broadening formations. The chart detector knows whether price broke above or below the boundary, but currently assigns the setup’s default `None` side. It should emit:

```text
close > upper boundary → side = +1
close < lower boundary → side = -1
```

Directionless forming patterns should not create meta-events.

## Phase 2: Separate model inputs from auxiliary data

Create two explicit schemas.

### Model inputs

Remove:

- `close`
- `ema_value`
- `atr_at_signal`
- `atr_at_bar`
- `volume_z`

Keep normalized equivalents such as:

- `atr_pct`
- `dist_ema_atr`
- `ema_slope_atr`
- `atr_change_ratio`
- ATR-normalized distances
- percentage candle anatomy
- `shape_48`

### Auxiliary columns

Retain but never send to the estimator:

- `atr_at_signal`
- signal close
- next open
- entry price
- exit price
- spread/slippage
- event start/end timestamps
- realized gross and net R

The present contract combines these concepts in [features.py](/Users/nishimweprince/Documents/Markets/Apps/trading-algos/lookup-trader/server/app/ml/outcome/features.py:17). Splitting them prevents leakage and extrapolation without breaking cost calculations.

Also canonicalize direction:

- Convert bullish/bearish trend categories into favorable/adverse relative to `side`.
- Multiply signed distance and slope features by `side`.
- Swap upper/lower distance pairs for shorts.
- Reflect each short-side shape bar from `[O,H,L,C]` to `[-O,-L,-H,-C]`.

That prevents the model from learning “gold rises” instead of “this setup is favorable in its proposed direction.”

## Phase 3: Perform one clean rebuild

After the data, tag-side, and schema changes:

```bash
.venv/bin/python scripts/rebuild_pipeline.py --yes --skip-train
```

Do not use plain `--yes` yet.

Then generate a feature audit containing:

- Fires per setup, year, side, and state.
- Complete versus forming counts.
- Constant and all-zero columns.
- Features firing below 0.1%, 0.5%, and 1%.
- Setup persistence across consecutive bars.
- Opposing tags on the same bar.
- Feature distribution drift by year.
- Candidate event count after deduplication.

Only retain tag columns for implemented setups that appear in development data. Persist that selected schema in the artifact rather than recomputing it during inference.

## Phase 4: Build the automatic meta-event dataset

The base strategy supplies events; the meta-model decides take or skip.

For every feature-store row:

```text
1. Read complete tags with a non-null side.
2. Detect a new completion transition; do not re-emit a persistent chart tag.
3. Group all tags with the same timestamp and side into one candidate.
4. Apply a setup cooldown/state-reset rule.
5. Enter at the next executable price.
6. Run the 1 ATR stop / 1.5 ATR target / 24-bar barrier.
7. Subtract spread and slippage.
8. Write y_meta = 1 if realized_net_r > 0, otherwise 0.
```

A practical event row would include:

```text
event_id
symbol
timeframe
signal_ts
entry_ts
exit_ts
side
primary_setup_id
all_setup_ids
tag_confidence
normalized causal features
atr_at_signal          auxiliary only
gross_r                label metadata
cost_r                 label metadata
net_r                  label metadata
y_meta                  0 or 1
event_version
tagger_version
label_version
```

For timeouts, use realized net R at the vertical barrier rather than automatically calling every timeout a failure. A timeout finishing at `+0.3R` after costs should differ from one finishing at `−0.3R`.

Useful training weights are:

```text
sample_weight = normalized_uniqueness × clipped_abs_net_r
```

The first term reduces the effect of concurrent events; the second gives economically significant outcomes more influence without allowing a few extreme trades to dominate.

### Deduplication rules

- Multiple bullish tags on one candle become one long event with confluence features.
- Multiple bearish tags become one short event.
- Long and short candidates may coexist initially, but a deterministic conflict policy must be tested.
- Repeated chart-pattern detections should emit only on `forming → complete`.
- A candlestick setup can emit immediately because it is naturally a one-bar event.

## Phase 5: Manual meta-labeling as an optional second source

The existing signal flow already stores manual setup and side with a causal context snapshot in [signals.py](/Users/nishimweprince/Documents/Markets/Apps/trading-algos/lookup-trader/server/app/services/signals.py:125).

For manual use:

1. Replay history in blinded mode.
2. Mark only moments where you genuinely see a trade candidate.
3. Choose setup and direction.
4. Record optional causal judgment fields such as confidence, key level, or confluence.
5. Let the triple-barrier engine assign the result automatically.

You should manually identify the candidate—not manually choose the outcome after seeing the future.

Manually reviewing all 102,860 bars is unnecessary. A better use of manual work is:

- Audit several hundred automatically detected events.
- Sample across every year, regime, setup, and side.
- Record false detections and missing setups.
- Create a smaller discretionary event population only if you want the model to learn your personal selection process.

Keep `source=automatic` and `source=manual` separate. First report results separately; combine them only after proving their conditional outcome distributions are compatible.

## Phase 6: Weighting and chronological evaluation

Compute concurrency using each event’s actual `[entry_ts, exit_ts]` interval:

```text
concurrency(t) = number of active events at t
uniqueness(event) = mean(1 / concurrency(t)) over its lifetime
```

Normalize training weights to mean 1. Report effective sample size separately.

Use expanding chronological folds with a global 48-bar purge:

- 2009–2013 → 2014
- 2009–2014 → 2015
- Continue through 2024
- 2025–June 2026 → historical audit
- Post-freeze Capital data → genuinely unseen forward test

The meta-model baseline must also change. Compare against:

- Take every eligible base event.
- Global event win frequency.
- Smoothed `setup × side × session × trend × ATR bucket` frequency.
- Regularized logistic meta-model.

The old every-bar `context_frequency` baseline is not the correct comparison population for sparse candidate events.

## Phase 7: Model progression

Train in this order:

1. Rule-only “take every event.”
2. Context-frequency event baseline.
3. Regularized logistic meta-model.
4. CatBoost with native categoricals and chronological Optuna tuning.
5. LightGBM challenger.
6. TabPFN challenger.
7. Out-of-fold probability blend only if it improves all gates.

Optimize chronological validation log loss first, then select the frozen take threshold using net R. Do not tune model parameters directly against final-test net R.

Promotion should require:

- Better log loss and Brier than the event-frequency baseline.
- Positive net R after conservative costs.
- Positive block-bootstrap confidence bound or sufficiently convincing uncertainty.
- Stability across years, sides, and setups.
- No single year/setup producing most of the profit.
- Acceptable drawdown under actual position-overlap rules.
- A meaningful abstention rate.

## Phase 8: Expanding from one pair to twenty

Do not immediately build 20 independent models. Use a pooled, normalized event model unless evidence supports specialization.

### Required generalization work

The current exporter hard-codes XAUUSD H1 and its barrier contract in [export_bar_features.py](/Users/nishimweprince/Documents/Markets/Apps/trading-algos/lookup-trader/server/app/services/export_bar_features.py:37). Replace this with a universe manifest:

```yaml
timeframe: H1
symbols:
  - XAUUSD
  - EURUSD
  - GBPUSD
contract:
  target_atr: 1.5
  stop_atr: 1.0
  horizon_bars: 24
```

For every pair:

- Validate candle completeness and timezone convention.
- Define pip size, spread, slippage, and market hours.
- Generate H4 consistently.
- Use the identical feature/tagger versions.
- Include `symbol` as a categorical model feature.
- Keep every numerical model feature scale-free.
- Split all pairs on the same calendar boundaries.
- Compute uniqueness within symbol, then simulate portfolio concurrency across symbols.
- Report pair-specific calibration and expectancy.

Start with XAUUSD, then 3–5 diverse liquid pairs, then expand to 20. With multiple pairs, the pooled model learns general setup behavior while pair-specific empirical priors and thresholds can shrink toward the global estimate.

## Recommended implementation order

1. Repair the 2023 source.
2. Fix directionless completed tag sides.
3. Separate model inputs from auxiliary execution columns.
4. Define next-open entry and net-R labels.
5. Bump the tag/feature/label contract versions.
6. Rebuild once with `--skip-train`.
7. Audit tag population and remove dead features.
8. Implement the automatic meta-event exporter.
9. Add event concurrency and uniqueness weights.
10. Run rule-only, frequency, and logistic baselines.
11. Add CatBoost/LightGBM only after the meta baseline report.
12. Historical portfolio backtest.
13. Capital Demo forward shadow.
14. Generalize the proven pipeline to additional pairs.

The next concrete build should therefore be **data repair + feature-contract correction + directional meta-event generation**, followed by one full rebuild—not training the current every-bar model again.