# Changelog

## Phase 0 — Skeleton

- Repo scaffold: npm + ESM + TypeScript 5 (strict), native TS execution via
  `node --experimental-strip-types`, vitest.
- `config/` — zod-validated config schema (Section 9) with env-var interpolation
  and centralized secret reads. `rpc`/`jito` optional for paper boot, required
  for live.
- `core/bus.ts` — typed in-process event bus over a discriminated-union event map.
- `core/types.ts` — shared, dependency-free domain types (graduation, verdict,
  position FSM, exit triggers).
- `core/constants.ts` — pinned pump.fun / PumpSwap / Raydium program IDs
  (overridable), program whitelist for the risk manager.
- `core/logger.ts` — structured JSON logger with registered-secret redaction.
- `core/lock.ts` — single-instance lock (atomic O_EXCL, stale-pid reclaim) to
  prevent double-trading.
- `persistence/` — SQLite schema (Section 10) via better-sqlite3 + repository
  layer + price-tick pruning.
- `alerts/telegram.ts` — Telegram alerter (grammy) with logging-only fallback
  when unconfigured; startup message.
- `index.ts` — bootstrap wiring config → lock → DB → bus → alerts, graceful
  shutdown, live-mode guards.
- Tests: config validation, event bus, lock lifecycle, persistence, secret
  redaction.

**Deliverable:** boots, validates config, sends a startup Telegram message.
