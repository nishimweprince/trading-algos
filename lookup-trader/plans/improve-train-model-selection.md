# Outcome Model and Meta-Labeling Roadmap

> Living design reference. Update this document when a phase changes the data,
> feature, event, label, model, or deployment contract. Detailed implementation
> plans should be written and approved one batch at a time.

## Current status

- XAUUSD H1 source candles have been imported for 2009-03 through 2026-07.
- The candle report contains 102,860 unique bars and no persisted duplicates.
- The 2023 source requires repair or quarantine before it is accepted for training.
- HistData volume is zero throughout, so volume-derived fields are not model inputs.
- The Batch 1 rebuild is complete: 102,661 causal feature rows and 25,332
  immutable automatic meta-events are available locally. No model was trained.
- The current outcome-v1 trainer is retained only as a reference and must not be
  promoted or rerun as the new training solution.
- Batch 2 is complete: the meta-model package, chronological folds, four
  candidates and the baseline report all exist and run offline. **No artifact is
  installed and no endpoint is wired**, so `GET /outcome-model/shadow` still
  returns 503 and the Evidence panel still reads "unavailable" — deliberately,
  until something clears the promotion gates.
- The `data/models/outcome/` directory no longer exists. `server/.env` still pins
  `LOOKUP_OUTCOME_ARTIFACT_VERSION` to a deleted artifact; that is expected, and
  the artifact must not be restored (it was trained on the every-bar dataset with
  raw `close`/`ema_value`, and would fail `infer.py`'s `bar_feature_version`
  contract check anyway — it was built at 1.2.0, live is 1.4.0).

### Batch 1 implementation status (2026-08-06)

The code and 17-year rebuild for the first batch are complete. The frozen contracts are now:

- Candle audit v1: completeness below 90% plus at least three unexpected gaps;
  February–July 2023 are quarantined and dependency padding is 48 bars before /
  600 bars after. Source candles are never deleted.
- Bar features `1.4.0`, meta features v1, meta labels **v2**, and manifest v1.
- Automatic entry at next H1 open, **2 ATR stop, 3 ATR target** (1.5 R:R),
  24-bar horizon, conservative same-bar loss, and 3/5/8-pip cost scenarios.
- Every `_r` column is denominated in **R — multiples of the stop distance**, not
  in ATR. The two coincided only while the stop was 1 ATR.
- Opposite-side conflicts retain the strongest tag confidence; exact ties emit
  neither side and are counted in the audit manifest.
- The immutable event export is `data/exports/meta_events_v1.parquet`; reviews
  live separately in DuckDB and never change v1 labels or inclusion.
- Replay candles are server-paged at 2,048 rows, client memory is bounded to
  three pages, and chart rendering is capped at 1,000 revealed candles.
- The Automated events screen is causal-first: outcome data is unavailable until
  a detector-validity verdict is stored and the separate reveal endpoint is used.

### Batch 2 implementation status (2026-08-07)

Phase 7 steps 1–4, offline. Everything below exists on disk and is under test.

| path | contents |
|---|---|
| `server/app/ml/meta/features.py` | 39 inputs split 9 categorical / 29 numeric / `shape_48`; `is_outcome_column` deny-list |
| `server/app/ml/meta/folds.py` | calendar-year expanding folds, interval-aware purge, vectorised `assert_no_overlap` |
| `server/app/ml/meta/baselines.py` | `TakeAll`, `EventFrequency`, meta preprocessor |
| `server/app/ml/meta/metrics.py` | binary scoring, net-R threshold sweep, block bootstrap |
| `server/app/ml/meta/training.py` | candidates, OOF evaluation, Optuna, audit-once orchestration |
| `scripts/train_meta_model.py` | CLI: `--dry-run`, `--no-tune`, `--trials N` |
| `server/tests/test_meta_model.py` | 27 tests |
| `data/reports/meta-baseline-XAUUSD-H1-v1.json` | report output |

`catboost>=1.2,<2` and `optuna>=4.0,<5` added to `server/pyproject.toml`.
Suite is 345 tests green. All three candidates give bit-identical predictions
across repeated runs.

**Deliberately a separate package from `app/ml/outcome/`.** That one is built on
a three-class `CLASS_ORDER` and a 137-column `INPUT_FEATURES`, both module
constants baked into function bodies, and two live consumers validate against
them (`outcome/infer.py`, `services/data_health.py`). Reused verbatim:
`outcome/artifact.py`, `outcome/preprocessing.py::_shape_values`,
`services/purged_cv.py::timeframe_delta`. Deliberately **not** reused:
`outcome/metrics.py::_expectancy` (hard-codes 1.5R/−1.0R and divides by raw ATR,
which assumed a 1 ATR stop) and `reliability_data` (indexes labels through the
three-class order and raises on 0/1).

Folds: 11 expanding calendar years, training 7,075 → 21,441 events, testing one
year each 2014–2024, purging the 0–23 events per fold whose trade was still open
when the test year opened. 2025–2026H1 (2,311 events) is the audit block.

#### Results — untuned run

| candidate | log loss | AUC | take % | OOF lift vs take-all |
|---|---:|---:|---:|---:|
| take_all | **0.67919** | — | 100% | — |
| event_frequency | 0.68292 | 0.512 | 6.9% | +0.0649 |
| logistic | 0.68351 | 0.505 | 9.0% | +0.0387 |
| catboost | 0.67988 | 0.518 | 14.8% | +0.0427 |

**No candidate beats `take_all` on log loss**, and AUCs of 0.505–0.518 mean
almost no discriminative power. Audit block, threshold 0.46 frozen beforehand:
take-all +0.0335R, selected +0.1335R on 317 of 2,311 events, **lift +0.1000R**.
Both audit years positive (2025 +0.0703R, 2026 +0.1660R); per-fold lift positive
in 7 of 11 folds (mean +0.0455, t = 1.76, p ≈ 0.079, and that understates it
because the threshold was chosen in-sample).

Two independent significance tests disagree, so this is **suggestive, not
established**:

- permutation over 20,000 random subsets of the same size: **p = 0.046**
- bootstrap 95% CI on the lift: **[−0.0133, +0.2124] — includes zero**

The tuned run (`--trials 40`) was still executing when this was written; rerun
and replace these numbers.

## Architectural direction

Automatic meta-labeling is feasible with this codebase; 17 years do not need to
be labeled manually. The current taggers already emit setup, state, confidence,
and usually direction through `BarTag.side` in
[`types.py`](../server/app/taggers/types.py).

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
- Stop: 2 ATR measured at signal time.
- Target: 3 ATR (1.5 R:R retained).
- Maximum holding period: 24 H1 bars after entry.
- Ambiguous bar: loss/conservative.
- Exit at horizon: mark to market.
- Costs: 3-pip XAUUSD round-trip primary assumption, with 5- and 8-pip sensitivity scenarios.
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

Several implemented patterns are also directionless in
[`setups_seed.py`](../server/app/db/setups_seed.py), including symmetrical
triangles, rectangles, and broadening formations. The chart detector knows
whether price broke above or below the boundary, but currently assigns the
setup’s default `None` side. It should emit:

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

The present contract combines these concepts in
[`features.py`](../server/app/ml/outcome/features.py). Splitting them prevents
leakage and extrapolation without breaking cost calculations.

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
6. Run the 2 ATR stop / 3 ATR target / 24-bar barrier.
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
- Opposite sides retain the side whose strongest tag has higher confidence; an exact tie drops both.
- Repeated chart-pattern detections should emit only on `forming → complete`.
- A candlestick setup can emit immediately because it is naturally a one-bar event.

## Phase 5: Manual meta-labeling as an optional second source

The existing signal flow already stores manual setup and side with a causal
context snapshot in [`signals.py`](../server/app/services/signals.py).

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

The current exporter hard-codes XAUUSD H1 and its barrier contract in
[`export_bar_features.py`](../server/app/services/export_bar_features.py).
Replace this with a universe manifest:

```yaml
timeframe: H1
symbols:
  - XAUUSD
  - EURUSD
  - GBPUSD
contract:
  target_atr: 3.0
  stop_atr: 2.0
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

Progress as of 2026-08-07. See **Next course of action** at the end of this
document for what to pick up.

1. ~~Repair the 2023 source.~~ — **quarantined, not repaired.** February–July
   2023 are excluded; 2023 holds 555 events against a ~1,516/yr average, so
   ~961 remain recoverable.
2. ~~Fix directionless completed tag sides.~~ Done — `patterns.py` emits
   `breakout_side`.
3. ~~Separate model inputs from auxiliary execution columns.~~ Done — 39 model
   features, 20 auxiliary.
4. ~~Define next-open entry and net-R labels.~~ Done.
5. ~~Bump the tag/feature/label contract versions.~~ Done — bar features 1.4.0,
   meta feature v1, meta label v2.
6. ~~Rebuild once with `--skip-train`.~~ Done.
7. ~~Audit tag population and remove dead features.~~ Done — 141 → 39.
8. ~~Implement the automatic meta-event exporter.~~ Done — 25,332 events.
9. ~~Add event concurrency and uniqueness weights.~~ **Descoped**, measured
   near-inert (concurrency 1.24, outcomes bimodal).
10. ~~Run rule-only, frequency, and logistic baselines.~~ Done.
11. ~~Add CatBoost only after the meta baseline report.~~ Done. LightGBM and
    TabPFN still deferred.
12. Historical portfolio backtest. — **not started**
13. Capital Demo forward shadow. — **not started**
14. Generalize the proven pipeline to additional pairs. — **not started**

Steps 1–11 are complete or consciously descoped. Nothing has been promoted: the
measured lift is not statistically convincing, so the next build is either the
economic calendar (Phase 9) or a larger event population, not another model.

## Batch-planning boundary

The first implementation batch should stop after the following deliverables:

1. A deterministic candle acceptance report that either accepts or quarantines
   the anomalous 2023 windows.
2. A frozen next-open, 2 ATR stop, 3 ATR target, 24-bar meta-event contract.
3. Correct breakout-side emission for every eligible completed tag.
4. Separate normalized estimator inputs and auxiliary execution/label columns.
5. An automatic meta-event export with versioned provenance and audit metrics.
6. One clean downstream rebuild with training explicitly skipped.

CatBoost, LightGBM, TabPFN, portfolio promotion, live order execution, and the
multi-pair universe are intentionally deferred to later implementation batches.

## Decision log

- 2026-08-06: Expanded the historical basis from 2024-2026 to 2009-2026.
- 2026-08-06: Kept the 24-bar triple barrier; overlap is addressed at the event
  population and evaluation layers instead of shortening the target to one bar.
- 2026-08-06: Replaced every-bar trade selection with sparse automatic
  meta-labeling; manual labeling remains an optional audited event source.
- 2026-08-06: Raw price and absolute ATR remain operational metadata but are
  forbidden estimator inputs.
- 2026-08-06: The first rebuild must use `--skip-train`.
- 2026-08-07: Widened the barriers to a 2 ATR stop / 3 ATR target, keeping 1.5
  R:R and the 24-bar horizon, and rewrote `meta_events_v1` in place under
  `meta_label` v2. A 1 ATR stop is about one average hourly range and was being
  taken out by noise: loss rate 60.4% → 51.3%, gross expectancy −0.020R → +0.002R,
  net −0.111R → −0.044R per event, total −2,814R → −1,107R. The bar the
  meta-model must clear fell 61%.
- 2026-08-07: Restated every `_r` column in R rather than ATR. A win is now
  exactly +1.5R and a loss exactly −1.0R at any stop width; cost is
  `spread × pip / (atr × stop_atr)`, so widening the stop genuinely dilutes it.
  `y_meta` is unchanged — the two scales differ by a positive constant.
- 2026-08-07: Did **not** take the best cell of the barrier sweep. The grid is
  monotone in both stop and reward with no interior optimum, which is cost
  dilution plus timeouts absorbing losses rather than an edge — at a 3 ATR stop
  40.8% of events never touch a barrier. 2 ATR was chosen because it was stated
  before the sweep and keeps the contract a triple barrier.
- 2026-08-07: Extending the horizon does **not** help. 24 → 96 bars collapses
  timeouts from 16.7% to 0.6% and leaves net R flat at −0.043. Keep 24.
- 2026-08-07: Phase 6's two weighting terms measure as near-inert and should be
  descoped. Mean event concurrency is 1.24 (median 1), so uniqueness weighting
  moves effective n only from 25,332 to 20,420; and triple-barrier outcomes are
  structurally bimodal, leaving `clipped_abs_net_r` almost nothing to weight.
- 2026-08-07: Tagger `confidence` does not predict outcome and must not be used
  as a pre-filter. Events at confidence ≥ 0.8 scored −0.1235R against −0.1047R
  below it (z = −1.19, not significant), and the threshold mostly swaps pin bars
  for engulfing patterns rather than selecting quality. Feed it to the model
  interacted with `primary_setup_id` instead.
- 2026-08-07: Kept the binary `net_r_3 > 0` target rather than going three-class
  or dropping timeouts. Three-class stays available as a challenger.
- 2026-08-07: **The audit block is the most favourable stretch in the sample and
  must never be scored on absolute net R.** Take-all earns +0.0335R over
  2025-2026H1 against −0.0515R over 2009-2024; only 3 of 18 years are positive
  (2009, 2025, 2026) and two of them are the audit block. Every figure is
  reported as lift over take-all on the same block.
- 2026-08-07: Built the meta-model as `app/ml/meta/`, a separate package from
  `app/ml/outcome/`, and wrote artifacts to a separate root. The outcome package
  hard-codes a three-class `CLASS_ORDER` and 137 `INPUT_FEATURES` that
  `outcome/infer.py` and `services/data_health.py` both validate against.
- 2026-08-07: Purge on each event's real `[entry_ts, exit_ts]` interval rather
  than `purged_cv.chronological_walk_forward`'s index arithmetic. At ~6 h mean
  event spacing its 48-index gap is about twelve days — conservative rather than
  leaky, but the wrong unit. Its `assert_no_leakage` is also O(n²) in Python and
  will not finish on 25k events; `meta/folds.py` has a vectorised replacement.
- 2026-08-07: Added an explicit outcome-column deny-list. `is_forbidden_feature`
  in `outcome/features.py` catches `y_meta` but returns **False** for `net_r_3` —
  that column was excluded only by absence from an allow-list.
- 2026-08-07: ForexFactory weekly URLs work and cut the backfill from ~6,300
  requests to ~900. The existing parser finds zero events in real markup, and
  stamps `America/Chicago` times as UTC — a 5-6 h, DST-varying error. No
  scraping service is needed; markdown extraction would lose the impact class.

## Resolved: timeout labels

**Decided 2026-08-07 — keep the binary target `net_r_3 > 0`, noise accepted.**
Widening the stop moved timeouts from 0.8% to 16.7% of events, and 61.3% of them
label positive, so 24.2% of all positive labels are marked to market rather than
barrier touches. Three-class remains available as a challenger if the binary
model's calibration stays worse than `take_all`.

Live execution still needs an explicit 24-bar exit rule, which barely mattered
at 0.8%. **Not yet implemented.**

## Phase 9: Economic calendar (probe complete, build not started)

Probed 2026-08-07 over June–July 2026. Weekly fetching works; the existing
parser does not. `data/calendar/events.parquet` still does not exist.

**Weekly URLs are viable.** `https://www.forexfactory.com/calendar?week=jun1.2026`
returns HTTP 200, 350–550 KB, server-rendered (not a JS shell). Nine requests
yielded 807 events across 55 dates. That takes a 2009–2026 backfill from ~6,300
daily requests to **~900 weekly** ones. `robots.txt` has no `Disallow` rules.
Firecrawl or similar is unnecessary — and would actively hurt, because markdown
extraction discards the CSS class that encodes impact.

**Three defects block reuse of `app/services/calendar/forexfactory.py`:**

1. **It extracts zero events from real markup.** It was written against
   `server/tests/fixtures/forexfactory_jul17_2024.html`, 402 bytes of
   hand-written HTML with `<td>High</td>` as literal text. Real pages nest 11
   cells and render impact as a span class.
2. **Impact is a CSS class, never text**: `icon--ff-impact-red|ora|yel|gra` →
   high | medium | low | non-economic.
3. **Times are `America/Chicago`, and line 62 stamps them as UTC.** The page
   embeds `'Timezone': 'America/Chicago'`; ISM Manufacturing releases 10:00
   New York and the page shows 9:00am. The error is **5 h in summer, 6 h in
   winter**, with 35 DST transitions across 2009–2026 — so a fixed offset is
   wrong half of every year. Parse with `ZoneInfo("America/Chicago")` and
   convert. A `mins_to_next_high_impact` feature built on the current code would
   be wrong *inconsistently*, which is worse than having no feature at all.

Date and time cells render only when they change, so a weekly parser must carry
both forward from `calendar__row--day-breaker` rows. A working prototype lives at
`scratchpad/ff_weekly_probe.py` (not yet promoted into `server/`).

**Causality split — freeze this before adding actual/forecast.** The markup also
carries `calendar__actual`, `calendar__forecast` and `calendar__previous`.
Schedule fields (time, currency, impact, title) are published in advance and are
safe features at any bar before the event. Actual/forecast/previous are known
only at release and must never be read before `time_utc`.

**Coverage must be audited before the features are trusted.** A missing calendar
day must not look like a calm day, or the model learns "old = no news" — the same
era-memorisation failure as raw `close`. Needs a per-day `calendar_coverage_ok`
flag, with uncovered days excluded or given an explicit missing category.

Also: `calendar_symbol_currencies["XAUUSD"] = ["USD"]` in `config.py` is probably
too narrow — gold responds to EUR and CNY events and to geopolitical risk.

## Next course of action

In order. Items 1–2 close out Batch 2; 3–5 are the next batch.

1. **Rerun `scripts/train_meta_model.py --trials 40`** and replace the untuned
   numbers above. Judge on `lift_vs_take_all`, never absolute net R.
2. **Decide on promotion.** Current evidence does not clear the Phase 7 gates:
   log loss is worse than `take_all`, and the lift CI includes zero. The honest
   default is *do not promote*, leave the Evidence panel unavailable, and treat
   the +0.10R audit lift as a hypothesis for the forward test rather than a
   result. Do not tune further against the audit block — it has been read once.
3. **Build the calendar backfill** — fix the three parser defects, add the
   coverage audit, crawl ~900 weekly pages at a polite rate into
   `data/calendar/events.parquet`.
4. **Add news features** at `meta_feature` v2 and re-export:
   `high_impact_in_horizon` (count within the next 24 H1 bars — the trade's
   actual lifespan, and the most promising of the set),
   `mins_to_next_high_impact`, `mins_since_last_high_impact`,
   `in_pre_news_window`, `in_post_news_window`, `high_impact_count_today`.
   Re-run the same four candidates and compare like for like.
5. **If news adds nothing**, the constraint is the event population, not the
   features. Then: repair 2023 (~961 recoverable events), implement the
   key-level detectors (`key_level_breakout`/`key_level_approach` are seeded
   vocabulary with no detector), or add H4 events. 94% of the current population
   is five candlestick rules, all with negative expectancy.

Deferred deliberately: LightGBM and TabPFN challengers, uniqueness weighting
(measured near-inert), the shadow endpoint, live execution, and the multi-pair
universe.
