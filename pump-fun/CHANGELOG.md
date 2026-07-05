# Changelog

## Phase 1 — Detection

- `detector/feed.ts` — `DetectionFeed` interface; the detector is feed-agnostic
  so PumpPortal (free) and gRPC (paid) are interchangeable.
- `detector/pumpportal.ts` — PumpPortal WebSocket feed (Node global `WebSocket`,
  no `ws` dep) via `subscribeMigration`, with exponential-backoff reconnect and
  defensive, self-documenting payload parsing.
- `detector/grpcStream.ts` — Yellowstone gRPC feed placeholder implementing the
  same interface; surfaces a clear "needs paid plan + client" error until built.
- `detector/dedupe.ts` — cross-feed dedupe by mint with TTL (Section 4.2).
- `detector/latency.ts` — rolling p50/p95/max detection-latency stats.
- `detector/index.ts` — orchestrator: feed → dedupe → on-chain confirmation →
  bus + decision log; debounced stream-health signal (grace window) that pauses
  entries when all feeds are down.
- `core/rpc.ts` — minimal JSON-RPC client (global fetch) for confirmation, with
  API-key redaction. Free Helius tier only.
- Config: `rpc` relaxed (only `primaryHttp` required); new `detector` section
  (feed toggles, dedupe TTL, confirm-on-chain, reconnect, latency logging).
- Wired into bootstrap; graceful detector shutdown.
- Tests: dedupe (TTL + eviction), latency percentiles.

**Deliverable:** paper log of live graduations with latency stats. Verified live
against PumpPortal + free Helius RPC — real pump.fun graduations detected,
confirmed on-chain, and persisted.

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
