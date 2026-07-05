# Changelog

## Phase 2 — Enrichment + Guardrails

- `enrichment/` — parallel candidate enrichment under a global budget
  (Section 5), per-field unknowns policy:
  - `mint.ts` — dependency-free SPL / Token-2022 mint decoder incl. TLV
    extension parsing. Verified live against USDC/USDT (active authorities) and
    PYUSD (Token-2022 extensions).
  - `holders.ts` — largest-holder snapshot with owner resolution.
  - `index.ts` — `Enricher` (mint + holders + DAS metadata, 1500ms budget).
- `core/base58.ts` — hand-rolled base58 codec (reads pubkeys from raw accounts).
- `core/rpc.ts` — added getMultipleAccounts / getTokenSupply /
  getTokenLargestAccounts / DAS getAsset.
- `guardrails/` — the screening engine (Section 6):
  - **Live now:** H1 mint authority, H2 freeze authority, H9 Token-2022
    extensions, H8 mint-blacklist, H10 kill-sentinel.
  - **Pending a verified PumpSwap pool decoder (Phase 2b) / swap-sim (Phase 4):**
    H3 LP, H4 sellability, H5 concentration, H6 creator, H7 liquidity — each
    returns `unknown` (→ veto in live per the unknowns policy; logged in paper).
  - `scoring.ts` — soft-signal score → position-size multiplier.
  - `engine.ts` — verdict aggregation + unknowns policy (live: unknown == fail).
  - `pipeline.ts` — graduation → enrich → evaluate → persist verdict → bus.
- Wired into bootstrap (runs when RPC configured); verdict rows persisted with
  full check results for tuning.
- Tests: base58, mint decode (authorities + Token-2022), engine verdict
  (paper-accept vs live-veto vs hard-fail vs blacklist), scoring.

**Deliverable:** every live graduation gets a persisted verdict row. The
mint-authority / freeze-authority / Token-2022 checks — the classic
infinite-mint and honeypot vectors — are enforced live on the free tier.

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
