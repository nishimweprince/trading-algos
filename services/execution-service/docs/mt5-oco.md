# MT5 OCO operations

Enable `MT5_OCO_ENABLED=true` in an execution-service HFM deployment to accept new OCO groups.
`MT5_OCO_POLL_SECONDS` defaults to 0.25 and is bounded at 5 seconds. The terminal must match the
configured login and use a hedge account (`margin_mode=2`). Netting is unsupported. The symbol
must advertise specified expiry (`expiration_mode & 4`). These checks follow the official
[MT5 symbol flags](https://www.mql5.com/en/docs/constants/environment_state/marketinfoconstants).
New groups are blocked until startup recovery and a recent inventory scan succeed.

Backtesting-service must explicitly select `ENTRY_MODE=oco_bracket` and
`MT5_OCO_EXECUTION=broker_pending`. Its protected market compatibility path remains available
for `synthetic_breakout`, or explicitly configured `local_market` OCO observation.

Every route below requires the configured API key. Legacy `/v1/signals` bodies and hashes are
unchanged. The canonical cTrader routes retain their existing behavior.

| Method | Route | Purpose |
| --- | --- | --- |
| GET | `/v1/mt5/capabilities?symbol=XAUUSDb` | Version, profile, capabilities, readiness and monitor/recovery reason. |
| GET | `/v1/mt5/inventory` | Actual account orders and positions, including unrelated exposure for diagnostics. |
| POST | `/v1/mt5/oco` | Reserve and submit a bracket using a stable group UUID. |
| GET | `/v1/mt5/oco/{group_id}` | Durable lifecycle, actual fills, accounting, protection and cancellation timing. |
| POST | `/v1/mt5/oco/{group_id}/cancel?reason=operator` | Cancel owned resting legs; retain filled positions and unresolved outcomes. |
| POST | `/v1/mt5/oco/{group_id}/close` | Explicit operator cleanup of that group's owned hedge positions and resting orders. |
| POST | `/v1/mt5/oco/{group_id}/acknowledge` | Clear an incident after inventory/history prove all group exposure settled. |

A group request contains `group_id`, `profile`, timezone-aware `occurred_at`, `decision_at`,
`expires_at`, `symbol`, `volume`, `upper_trigger`, `lower_trigger`, `stop_distance`,
`target_distance`, `source`, and `protection_policy=fill_relative`. Both legs are preflighted
before dispatch. Requests and decisions obey signal freshness limits. Reusing a group ID with
a changed body returns an idempotency conflict. An interrupted group never resends entry legs.

Submission success is distinct from order state. A `placed` leg is resting, not filled. Active
orders come from [orders_get](https://www.mql5.com/en/docs/python_metatrader5/mt5ordersget_py);
history confirms fills, cancellation and expiry. A missing cancellation target may have filled.
The monitor cancels a sibling on any confirmed entry volume, including partial fills, and cancels
the winning leg's unfilled remainder without topping it up. Equal broker fill timestamps choose
the long leg deterministically. Both-leg fills trigger an owned losing-position close and halt
new groups. An ambiguous compensating close is not blindly retried.

Each pending stop includes protection anchored to the pending entry. After a fill, the monitor
requests protection relative to actual average fill and verifies it in position inventory.
Failed or ambiguous amendment halts new groups; existing attached protection remains observable.
Requested protection requiring broker minimum widening is rejected during preflight. Price
precision rounding still applies. Group accounting recomputes broker profit, commission, swap
and fee from entry/exit deals; it does not replace the backtesting model P&L.

The group ledger uses `<signals database stem>.oco.sqlite3`, separate from the unchanged legacy
signal database. Back it up with SQLite-aware tooling. Preserve unresolved records and group IDs
through deployments and rollbacks. The monitor recovers existing groups even when new OCO entries
are disabled. Missing owned ledger records, account changes, unavailable inventory/history, and
uncertain dispatch block new groups. Inspect readiness and group status; do not delete the ledger
or change IDs to bypass recovery.

For an incident: cancel the group, inspect live inventory/history, explicitly close owned exposure
if desired, wait for terminal confirmation, then acknowledge. Acknowledgement is rejected while
positions, resting orders or unknown dispatch remain. Investigate missing-ledger orders manually;
the service reports them without guessing ownership or closing unrelated positions.

The bridge's broker expiry uses elapsed time with one parent-bar grace interval; engine expiry
counts eligible parent bars. Market closures can make the server watchdog expire first. Server
expiry survives either process being offline; fill/cancel races still require reconciliation.

Do not activate a running live profile merely to validate payloads. Use shadow mode first.
`scripts/verify_mt5_oco.py` in backtesting-service probes account and symbol metadata by default.
Its explicit execution flag places bounded 0.01-lot-or-smaller test groups and cleans up only their
owned exposure. Record account mode, metadata, fill/expiry/restart results, cancellation latency,
protection deviations, and final owned inventory before enabling a strategy profile.
## Broker server clock

Group decision and expiry timestamps, freshness checks, and the watchdog use UTC.
Set `MT5_OCO_SERVER_UTC_OFFSET_SECONDS` to the verified terminal server offset
(default `0`). Pending-order expiration is converted to server time at submission;
the offset is retained with the group for fill-time normalization and recovery.
New entries require a quote whose normalized timestamp is between five seconds
in the future and sixty seconds old. A stale quote or incorrect offset blocks
submission. Recovery and cancellation remain available when that check fails.

On 2026-09-15, HFM `HFMarketsGlobal-Live20` quotes for `XAUUSDb` use UTC+3
(`10800` seconds). Verify this again after seasonal server clock changes; do not
infer an offset from a stale quote. History queries include both UTC and server
time representations and match only durable owned tickets. The legacy signal
path is unchanged. Run the bounded verification with
`--profile hfm --symbol XAUUSDb --server-offset-seconds 10800 --execute`.

The live verification accepted 120-second deadlines and rejected sub-minute
deadlines with retcode `10022` despite passing preflight. Treat server rejection
as a failed placement, with cancellation/reconciliation of any surviving leg.
Do not extend strategy expiry silently. The default verification duration is
120 seconds and can be set explicitly with `--expiry-seconds`.
