# mt5-trader is frozen

This service has been merged into `services/execution-service` as the `mt5`
broker adapter. The code here is kept, unchanged, so the Windows host can keep
running the old service until the live smoke passes on the new one.

Nothing here should be edited. Fixes go to
`services/execution-service/src/execution_service/adapters/mt5/`.

## Cutover

1. On the Windows host, install the new service with the MT5 extra:
   `uv sync --package execution-service --extra mt5`
2. Set `ADAPTERS=mt5` in the profile env file; keep `PORT` at 8000 (forex) or
   8001 (deriv) so `MT5_SIGNAL_API_URL` in lux-algo, ipda, signals-scrapper and
   lookup-trader needs no change.
3. Point `DATABASE_PATH` at the existing `signals.db`. Replay is gated by
   `SignalRequest.canonical_json`, which is byte-identical to this service's, so
   already-filled signals will not re-execute.
4. Confirm `/health/ready` and a `GET /v1/market-data/tick`, then submit one
   signal and check it appears in `signals.db` exactly once.
5. Only then delete this directory.
