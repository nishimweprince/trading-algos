# ta-store

The durable ledger that makes execution idempotent: an operation is reserved
before it is dispatched, replayed rather than re-executed if the same
`operation_id` arrives twice, and each per-account target settles independently
while the parent state is recomputed from its children.

Extracted from `ctrader-markets/src/execution_repository.py`, the richer of the
two ledgers in the repository. `mt5-trader/src/.../repository.py` was a second,
thinner implementation of the same idea over a flat `signals` table, and it was
missing `PRAGMA busy_timeout`, so a concurrent writer surfaced as an immediate
`database is locked` rather than waiting.

The parent-state rollup in `_refresh_parent` is the part worth reading: a
fan-out across accounts can half-succeed, which is why `PARTIAL_FAILURE` exists
and why a single boolean would be wrong.
