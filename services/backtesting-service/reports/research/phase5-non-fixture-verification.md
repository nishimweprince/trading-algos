# Phase 5 non-fixture verification

Status: **non-fixture programme delivered; Phase 5 remains incomplete**.

This artifact records the testing and reporting work that can be verified without the missing
historical M15, H1 and H4 export CSVs. It does not clear an export-derived acceptance criterion,
the Phase 3 gates, or a prop-firm claim.

## Reporting contract

Every backtest now returns one `report_header` with the entry mode, session anchors, stop and
target modes, target R, lock mode and distance, time exit and maximum age, risk and cost modes,
intrabar resolver and tier, quantity reference, firm profile, first and last accepted bar,
warm-up count, validation rejection counts, loaded M1 count and fallback count. The client echoes
that contract above the result.

Each completed structure reports stop pips, holding hours, session-local weekday and paired
gross/cost/net R alongside the existing paired gross/cost/net pips. The diagnostics surface adds:

- fixed-bin net-R and holding-time histograms;
- MAE/MFE leg scatter and a concurrent-structure timeline;
- per-session and per-weekday tables with gross/net pips and gross/net R; and
- a PropGuard panel that reports observed guard state and breach days, while labelling worst
  simulated day and broker free-margin/headroom paths unavailable. Those require the separate S7
  simulation and broker margin observations and are not fabricated in the interactive backtest.

The presentation-only header and structure analytics are explicitly removed from the Phase 1
golden payload before hashing. The original golden hash remains bit-for-bit unchanged.

## Executable testing programme

`tests/test_phase5_programme.py` supplies deterministic property-style coverage for stop fills,
OCO stop-entry fills, OHLC bounds and duplicate-bar double-close prevention. It also runs the
complete currently executable configuration product:

```text
4 ENTRY_MODE × 2 STOP_MODE × 1 TP_MODE × 1 LOCK_MODE × 4 INTRABAR_MODE = 32 cells
```

Every cell validates its `EngineParams`, runs the committed internal candle fixture, echoes its
configuration, and checks both `net = gross - cost` identities in pips and R plus the execution
plus financing cost identity. Tick mode remains an explicit unavailable interface and has a
separate rejection test rather than being silently omitted.

Verification on 2026-08-20:

```text
.venv/bin/ruff check src tests                       PASS
.venv/bin/pytest -q -rs                             330 passed, 5 skipped
cd client && npm test -- --run                      21 passed
cd client && npm run build                          PASS
git diff --check                                    PASS
```

The build emits one non-failing warning that the main JavaScript chunk exceeds 500 kB after
minification. No type, test, lint or build error is present.

## Intentionally unresolved fixture criteria

All five external-fixture checks remain collected and explicitly skipped:

1. W1.1 cost acceptance: M15/H1 export fixtures absent.
2. M15/H1 known-metric regression: export CSVs absent.
3. H4 known-metric regression: export CSV absent.
4. S5 M15/H1/H4 resolver calibration: export CSVs absent.
5. W1.2 H1 fixed-sizing acceptance: H1 export fixture absent.

Therefore Phase 5 remains unchecked. The known export figures, cross-timeframe S5 rates and
fixture-derived sizing/cost criteria are **unverified**, not failed, passed, inferred or
reconstructed.

## Production delta

This delivery changes reporting and tests only. Phase 0–2 trading behaviour, research parameters
and committed research surfaces move by **0.0 gross pips / 0.0 gross R** and
**0.0 net pips / 0.0 net R**.
