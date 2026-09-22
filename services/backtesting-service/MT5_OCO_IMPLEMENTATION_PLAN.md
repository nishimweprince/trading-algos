# MT5 OCO breakout implementation plan

## Objective

Allow the HFM profile to run `ENTRY_MODE=oco_bracket` with MT5 execution while preserving the opening-range triggers, expiry, stop sizing, and fixed-R target defined by that strategy. Do not substitute `synthetic_breakout`: its triggers and absolute exit levels implement a different strategy.

Implementation and verification are complete. See [MT5_OCO_IMPLEMENTATION_STATUS.md](MT5_OCO_IMPLEMENTATION_STATUS.md) for evidence, existing regression failures, and rollout settings. Active `.env.hfm` files remain unchanged. The user explicitly authorized bounded 0.01-lot verification on the configured live account, replacing the proposed demo-account verification.

## Findings at plan creation

- `.env.hfm` selects `oco_bracket` and live execution. `Settings._validate_execution_surface()` in `src/backtesting_service/config.py` rejects every MT5 entry mode except `synthetic_breakout`.
- `src/backtesting_service/mt5_execution.py` implements protected market entries and signal submission/status lookup through `/v1/signals`. It exposes no stop-entry builder, cancellation, position protection amendments, or broker inventory.
- `ExecutionBridge._on_staged()` in `src/backtesting_service/execution_bridge.py` places two broker stop entries for `oco_bracket`. Removing validation would reach methods the MT5 client does not implement.
- The existing MT5 market path submits on an engine `entry` event. Locally staged synthetic orders do not rest at the broker.
- The shared `SignalRequest` in `../../packages/ta-contracts/src/ta_contracts/signals.py` already accepts stop/limit entries, `entry_price`, protection distances, and `expires_at`. The MT5 service in `../execution-service/src/execution_service/adapters/mt5/service.py` already builds pending requests. The limitation is broader order lifecycle support and this client's interface, rather than an absence of pending-order fields.
- The execution-service canonical gateway is currently constructed for cTrader in `../execution-service/src/execution_service/api.py`. Its `/v1/orders` routes are not automatically an MT5 implementation.
- `placed` is currently normalized to operation success. Placement success is not evidence that an order filled. Signal records and submission reconciliation are insufficient to manage the ongoing OCO lifecycle.
- Existing MT5 distance protection is resolved into absolute prices before submission, using the pending entry or current quote. Actual fill gaps can therefore change realized stop/target distances. Existing bridge comments about fill-relative protection need to be checked against this behavior.

## Recommended approach and accommodation

| Approach | Behavior | Dependencies | Tradeoff |
| --- | --- | --- | --- |
| **A. Broker pending orders with execution-service OCO coordination (recommended)** | Rest both stops at MT5; execution-service watches broker fills and cancels the sibling independently of the strategy's bar loop. | MT5 inventory, cancellation, durable group state, fill reconciliation, and recovery. | Keeps pending-order execution; two submissions and sibling cancellation have a race window that must be handled explicitly. |
| **B. Locally monitored OCO with one market entry (optional first milestone)** | Keep `oco_bracket` staging and trigger decisions in the engine; submit one protected MT5 market entry when the engine emits `entry`. | Provider capability routing, persistent trigger/dispatch state, and explicit execution semantics. | Smaller implementation, but executes when the trigger is observed; current closed-bar observation can be much later than the price crossing. No pending orders exist during downtime. |

Implement B only behind an explicit setting, suggested `MT5_OCO_EXECUTION=local_market`. Implement A as `MT5_OCO_EXECUTION=broker_pending`. Preserve the existing MT5 restriction by default until a path is selected and supported. Never silently fall back between these paths.

For the first release, both paths retain `TP_MODE=fixed_r`, `BE_TRIGGER_R=0`, and `TIME_EXIT_MODE=none`. Other hedge modes, trailing/partial exits, break-even management, and general time-exit support remain separate work.

## Phase 1: Define execution capabilities and invariants

- [x] Introduce a typed execution-client protocol and capability description rather than relying on a union plus `type: ignore` and missing methods. Separate market entry, pending entry, cancellation, inventory, protection amendment, and OCO coordination capabilities.
- [x] Define the strategy/dispatch matrix in configuration validation: existing synthetic market execution; `oco_bracket` with explicitly selected local market execution; `oco_bracket` with a gateway that supports broker OCO. Reject unsupported combinations with actionable messages.
- [x] Validate configured capabilities locally and verify gateway capabilities/readiness before live dispatch. An unavailable or incompatible gateway blocks new orders; it does not trigger fallback.
- [x] Define distinct submission and broker lifecycle states: not submitted, dispatching, unknown, placed/resting, partially filled, filled, cancelled, expired, rejected. Keep placement operation success separate from resting-order state.
- [x] Give each bracket a stable group identity derived from symbol and engine pair ID. Persist intent before external submission and retain stable IDs for legs and control operations across restarts.
- [x] Preserve `EXECUTION_VOLUME_LOTS` as the broker size source; never derive it from accounting `QTY`.
- [x] Establish freshness rules for staged brackets and observed entries, including reconnect/catch-up bars. Use current UTC time for request age, but preserve decision and observation timestamps for auditing.
- [x] Specify protection policy: initially attach protection to each order; record requested and applied levels/distances. For A, reconcile actual fills and amend to the intended fill-relative OCO levels where supported. If this cannot be achieved, reject the strict path or require an explicit documented deviation policy. Do not describe entry-anchored protection as fill-relative.

Deliverable: capability matrix, request/response/state contracts, and configuration tests before loosening the MT5 guard.

## Phase 2B: Optional local-market accommodation

This milestone can ship independently while broker OCO support is developed.

- [x] For MT5 `oco_bracket` plus `local_market`, process staging and expiry locally without calling `build_stop_entry()` or broker cancellation methods. Preserve engine ORB high/low, buffer, eligible-bar expiry, and re-entry rules.
- [x] On the selected engine `entry`, build one protected market signal using the event's side, stop distance, and effective target R. Avoid assuming a global `RR` is always the event's target.
- [x] Handle the engine's event ordering: sibling cancellation can arrive before entry. Local sibling cancellation must not suppress the chosen market entry.
- [x] Persist dispatch intent and operation ID before submission. After timeouts or restart, reconcile the existing signal rather than issuing a fresh entry. Duplicate engine events must not submit twice.
- [x] Reject stale replay/catch-up entries under the freshness policy; record a skipped execution separately from a paper fill.
- [x] Keep engine model entry and broker execution price separate. Report observation delay, price difference, actual volume, and applied protection. Local strategy exits must not be treated as proof the broker position closed.
- [x] Preserve unresolved broker tracking after engine exits until execution outcome is known. Without inventory, expose broker lifecycle as unavailable rather than claiming convergence.
- [x] Document that the current closed-bar engine chooses the side using its intrabar rules. A tick-driven monitor would be another implementation milestone, with its own bid/ask, ordering, and replay semantics.

Acceptance: an explicitly configured local OCO generates one market signal for the selected side, preserves the strategy's staging rules, expires without dispatch, and survives replay/restart without duplicate entries.

## Phase 2A: Extend execution-service MT5 lifecycle support

- [x] Reuse pending request preparation, symbol mapping, precision/volume checks, and signal-age validation already present in the MT5 adapter/service.
- [x] Choose and document the API integration seam before coding: implement MT5 against the canonical gateway port, or add a narrowly scoped MT5 OCO API. Prefer reuse of canonical contracts where compatible; preserve existing `/v1/signals` payload hashing and replay semantics.
- [x] Add account-scoped live pending-order and position inventory, order/deal history reconciliation, cancellation, and protection amendments required by the fill policy. Extend MT5 adapter protocols and fakes accordingly.
- [x] Add a durable bracket/group contract containing both triggers, volume, protection policy, expiry, source, and stable leg IDs. A group operation should own coordination instead of leaving sibling cancellation to backtesting-service's bar events.
- [x] Preflight both legs before dispatch. If only one leg is placed, cancel it and reconcile the result; if it already filled, manage the resulting exposure. Persist every transition so a crash between legs can be recovered.
- [x] Monitor live orders and deals continuously while groups are active, independently of candle polling. Match ownership using durable group/leg identities and broker tickets; never manage unrelated manual orders.
- [x] On the first confirmed fill, cancel the sibling immediately and confirm its terminal state. Treat any partial fill as exposure requiring sibling cancellation. Define residual-volume handling explicitly, with no automatic top-up in the first release.
- [x] Handle fills during cancellation and both-leg fills. Select the winner deterministically from broker timestamps/tie rules, halt further group entries, and reconcile exposure. Any compensating close must use the actual account model and owned exposure; do not assume netting accounts retain two independently closable positions.
- [x] Validate account mode support. Initially reject unsupported netting/hedging behavior rather than risking interference with shared-symbol positions.
- [x] Validate server-supported expiry using official MT5 documentation and HFM demo symbol metadata during implementation. Use server expiry when available plus a gateway watchdog; reject broker OCO when required expiry guarantees are unavailable.
- [x] Treat cancel-not-found as an instruction to reconcile inventory/history, not unconditional proof of cancellation: the order may have filled.
- [x] Recover groups from durable state and live inventory/history at startup, including accepted pending orders absent from order history. Block new entries until unresolved dispatches and orphaned owned orders are reconciled.

Acceptance: execution-service manages a bracket through placement, fill, sibling cancellation, expiry, partial placement, disconnect, and restart without depending on a new engine bar.

## Phase 3A: Integrate broker OCO into backtesting-service

- [x] Extend `Mt5ExecutionClient` with the selected group/lifecycle contract and normalized broker IDs, position IDs, fill prices, and states. Enable inventory only once real inventory is implemented.
- [x] Route MT5 broker OCO staging through the group operation. Preserve the cTrader path while reviewing shared state assumptions introduced by MT5 changes.
- [x] Separate engine prediction from broker truth: broker fills select the live group winner; paper cancellation events must not cancel a broker-confirmed winning leg or manufacture a broker fill.
- [x] Define how broker fills enter live strategy state and accounting without duplicating paper opportunities. Expose prediction/execution differences when the paper engine chose a different side or price.
- [x] Apply engine expiry, structure closure, and prop-guard halt to the owned group. Confirm cancellation before removing resting-order tracking. Keep unresolved orders visible and block new entries when their risk is unknown.
- [x] Review `_expiry()` against eligible-parent-bar expiry and market closures. Document the engine expiry boundary and server watchdog deadline, including any grace interval, instead of assuming elapsed minutes are identical to eligible bars.
- [x] Extend `PaperTrader` persistence and startup reconciliation to include groups, lifecycle states, and unresolved control operations. Save lifecycle changes even when no new candle arrives; provide migration for existing market-only snapshots.
- [x] Extend `/v1/execution` and client types/views with execution path, group state, resting orders, actual fills, pending cancellations, applied protection, and divergence. A paper exit does not erase unresolved broker exposure.

Acceptance: HFM broker OCO starts successfully only against a capable gateway, places the intended ORB bracket, shows actual broker state, and recovers without duplicate or orphaned owned orders.

## Validation

Add behavior tests alongside existing tests; do not exercise the currently live `.env.hfm` as a smoke test.

| Area | Required cases |
| --- | --- |
| Configuration | Default rejection; explicit local path; capable broker path; missing capability/readiness; all retained exit restrictions. |
| Local execution | No broker staging/cancellation; one chosen-side market entry; expiry; cancellation-before-entry; re-entry; stale catch-up; repeated event; ambiguous submission and restart. |
| Gateway OCO | Both stops placed; one leg rejected; timeout between legs; crash after acceptance; partial fill; sibling fill during cancellation; both-leg fill; idempotent cancellation. |
| Protection | Price gaps; requested vs applied distance; broker minimum widening; precision; failed post-fill amendment; explicit policy enforcement. |
| Lifecycle | Live pending inventory plus history; expiration; offline monitor; startup orphan reconciliation; halt while cancel is unknown; engine exit before broker exit. |
| Compatibility | Existing synthetic MT5 behavior, signal hash/idempotency, cTrader bridge behavior, and old persisted snapshots. |

Suggested checks from this directory after implementation:

```powershell
uv run pytest tests/test_config.py tests/test_mt5_execution.py tests/test_execution_bridge.py tests/test_execution.py tests/test_entry_modes.py
uv run ruff check src/backtesting_service tests
```

Run the execution-service and shared-contract tests from their respective packages when modified. Run client type/build checks if execution response types change. Then validate the selected path in shadow mode and on an explicitly configured HFM demo account: both directions, no-fill expiry, process restart, gateway disconnect, and fill/cancel timing. Record account mode, symbol metadata, observed cancellation latency, and any protection deviations.

## Rollout and completion

1. Ship capability/state groundwork with existing MT5 defaults preserved.
2. Optionally release explicit `local_market` in shadow/demo, documenting closed-bar execution delay.
3. Implement and verify gateway OCO coordination before permitting `broker_pending` live dispatch.
4. Update `.env.example.hfm`, `README.md`, and the execution-service operator runbook with the selected path, retained restrictions, expiry/protection policies, and recovery procedure.
5. Change the active HFM profile only as part of a separate deliberate rollout. A rollback first reconciles owned orders and positions; switching configuration does not cancel broker exposure.

Completion requires passing lifecycle tests, a documented demo verification, explicit strategy/execution semantics, and no unresolved owned orders after recovery checks. Editing the configuration guard alone does not satisfy this plan.

## Implementation file map

- Backtesting-service: `src/backtesting_service/config.py`, `models.py`, `mt5_execution.py`, `execution_bridge.py`, `paper.py`, `api.py`; relevant execution/config/entry-mode tests; `client/src/lib/types.ts` and execution views if response types change; `.env.example.hfm` and `README.md`.
- Execution-service: `src/execution_service/adapters/mt5/mt5_adapter.py`, `service.py`, new group coordinator/repository as needed, `ports.py`, `api.py`, `compat.py` only where compatibility routing requires it, MT5 lifecycle tests, and `docs/mt5-operator-runbook.md`.
- Shared packages where needed: `../../packages/ta-contracts` for capabilities/group/lifecycle contracts; `../../packages/ta-clients` for reusable client support. Preserve existing legacy canonical serialization.

Execution-service changes are installed in the sibling service. Shared signal contracts remain unchanged. This plan records completed implementation; activating the strategy profile remains a separate rollout.
