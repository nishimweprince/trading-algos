---
name: Outcome Model Live Feed
overview: "Checkpoint after deterministic tagging, causal outcome training, and shadow inference. OANDA ingestion is partially implemented; live validation and the long-running shadow evaluator remain."
todos:
  - id: finish-tagging
    content: Complete and validate deterministic chart tagging, then add complete-state tag filtering to base rates.
    status: completed
  - id: build-dataset
    content: Create a causal feature allow-list, bar-by-side outcome dataset, and purged chronological splits.
    status: completed
  - id: train-model
    content: Train, calibrate, evaluate, and version the baseline and boosting outcome models.
    status: completed
  - id: serve-inference
    content: Add schema-checked causal inference and expose shadow predictions separately from recommendations.
    status: completed
  - id: sync-oanda
    content: Implement OANDA H1/H4 backfill and closed-candle incremental sync with provenance and parity checks.
    status: in_progress
  - id: shadow-gate
    content: Run outcome resolution and monitoring in no-trade shadow mode before any recommendation promotion.
    status: pending
isProject: false
---

# Outcome model and OANDA shadow checkpoint

## Completed

### Deterministic tagging and analytics

- Promoted chart geometry into [server/app/taggers/chart/patterns.py](server/app/taggers/chart/patterns.py), removed the duplicate candidate API, and wired it through [server/app/taggers/pipeline.py](server/app/taggers/pipeline.py).
- Added non-vacuous causal leakage tests, shared match-quality/state semantics, review exports, and updated tagging documentation.
- Added `tag_setup_id` and `tag_state=complete` filtering through [server/app/services/base_rate.py](server/app/services/base_rate.py), the API, client query types, and ComparePanel.

### Leakage-proof outcome dataset

- Added the causal feature contract and tag encodings in [server/app/ml/outcome/features.py](server/app/ml/outcome/features.py).
- Extended [server/app/services/export_bar_features.py](server/app/services/export_bar_features.py) to export both long and short outcome rows with completeness/reliability guards and a deterministic manifest.
- Added timeframe-aware purged walk-forward folds and a terminal untouched holdout in [server/app/services/purged_cv.py](server/app/services/purged_cv.py).

### Training and artifact

- Added logistic and histogram-gradient-boosting training, calibration, metrics, immutable artifact validation, and CLI support in [scripts/train_outcome_model.py](scripts/train_outcome_model.py) and [server/app/ml/outcome/](server/app/ml/outcome/).
- Exported 30,232 XAUUSD H1 side-rows and trained:
  `data/models/outcome/xauusd-h1-outcome-v1-pilot-20260805-r2`.
- Holdout checkpoint:
  - model log loss `0.7556`
  - context-frequency baseline log loss `0.7182`
  - Brier `0.4942` versus baseline `0.4951`
  - ECE `0.0516`
  - observed net expectancy `-0.0203R`
- The pilot is valid for pipeline/shadow testing but **must not be promoted**: it did not beat the empirical baseline and underestimates timeouts.

### Causal shadow inference

- Added artifact/schema/version-checked inference in [server/app/ml/outcome/infer.py](server/app/ml/outcome/infer.py) and `GET /outcome-model/shadow`.
- Added the separate, explicitly unpromoted Model Shadow readout in ComparePanel. It does not affect recommendation logic.
- Verified the installed r2 artifact end-to-end: the endpoint returned HTTP 200 with finite long/short `p_win`, `p_loss`, and `p_timeout`.
- Updated [scripts/dev.sh](scripts/dev.sh) to prefer the repository `.venv` Python. The currently running old process must be restarted to pick this up.

## In progress: OANDA closed-candle sync

Partial implementation already exists:

- Provider-neutral contract: [server/app/providers/base.py](server/app/providers/base.py)
- Read-only OANDA adapter: [server/app/providers/oanda.py](server/app/providers/oanda.py)
- Conflict-aware atomic publisher and provenance sidecars: [server/app/services/oanda_sync.py](server/app/services/oanda_sync.py)
- CLI: [scripts/sync_oanda.py](scripts/sync_oanda.py)
- View refresh helper: [server/app/db/duck.py](server/app/db/duck.py)
- Data/model health draft: [server/app/services/data_health.py](server/app/services/data_health.py)
- Credential-name template: [.env.example](.env.example)

This work was interrupted before verification. Treat it as unreviewed until the following are complete:

1. Add OANDA fixtures and tests for instrument validation, pagination, `complete=true` filtering, end-of-period timestamps, H1/H4 alignment, idempotent overlap, conflicts/quarantine, locking, provenance, gaps, stale feeds, and refresh behavior.
2. Wire `data_model_health()` into `/health` and test latest candle lag, H4 availability, feature/model versions, and store/live tag parity.
3. Add README setup/backfill/incremental commands and explain that OANDA midpoint candles may conflict with HistData pricing; conflicts must remain quarantined rather than overwritten.
4. Run credentialed checks without printing secrets:

```bash
.venv/bin/python scripts/sync_oanda.py --check-instrument
.venv/bin/python scripts/sync_oanda.py --symbol XAUUSD --timeframe H1 --start 2021-01-01T00:00:00Z --dry-run
.venv/bin/python scripts/sync_oanda.py --symbol XAUUSD --timeframe H4 --start 2021-01-01T00:00:00Z --dry-run
```

Required private values in `server/.env`:

```text
LOOKUP_OANDA_ENVIRONMENT=practice
LOOKUP_OANDA_TOKEN=...
LOOKUP_OANDA_ACCOUNT_ID=...
```

Do not paste credentials into chat or commit them.

## Remaining: shadow worker and promotion gate

1. Add a standalone, no-order shadow worker that polls shortly after H1 close, synchronizes H1/H4, updates causal features/tags, and records both directional predictions idempotently.
2. Store predictions separately from manual occurrences with key `(model_version, symbol, timeframe, ts, side)`, including probabilities, tags, empirical base rate, schema/data versions, and later outcome.
3. Resolve only after the 24-bar horizon using explicit candle `as_of_ts`, never `datetime.utcnow()`.
4. Wire the existing `revealedThrough` boundary before exposing any forward overlay in shadow UI.
5. Monitor feed lag/gaps, feature null/range drift, tag drift, calibration, timeout underprediction, expectancy after costs, and batch/shadow parity.
6. Keep recommendations empirical-only. Retrain after at least five years of consistent OANDA H1/H4 history, and require out-of-time baseline lift plus 8–12 weeks of stable shadow evidence before promotion.

## Verification and recovery checklist

After a cutoff, resume at the OANDA tests—not at tagging or training:

```bash
git status --short
.venv/bin/python -m pytest server/tests/test_outcome_export.py server/tests/test_outcome_model.py server/tests/test_outcome_infer.py -q
cd client && npm test -- --run && npm run build
```

Then finish the OANDA tests/docs/health wiring, run the full server suite, restart `./scripts/dev.sh`, and verify:

- `/context` serves stored tags where the rebuilt feature row exists.
- `/outcome-model/shadow` returns r2 probabilities with `promoted=false`.
- `/health` reports current candle/feature/model versions and parity.
- OANDA sync publishes no incomplete, duplicate, or conflicting candle.
- Shadow mode never invokes an order or trade-execution route.
