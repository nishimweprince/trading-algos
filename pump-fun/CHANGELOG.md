# Changelog

## Phase 4a — Execution foundation

- `core/rpc.ts` — global concurrency limiter (semaphore, default 4 in-flight) so
  the enrichment burst stays under the free-tier rate limit; **fixes the
  intermittent H5/H6 `unknown`** (holders now resolve 5/5 live). Config:
  `rpc.maxConcurrentRequests`. Added `getRecentPrioritizationFees`.
- Added `@solana/web3.js` (pure JS; builds on Node 26).
- `executor/wallet.ts` — keypair load from env (base58 or JSON array), log
  redaction, ephemeral fallback for dry-run, and the whitelist-only signing
  policy (Section 8: refuse to sign for non-whitelisted programs).
- `executor/fees.ts` — priority fee (p75 of recent fees, capped) + Jito tip plan.
- `executor/broadcaster.ts` — the run-mode gating keystone: paper throws (no tx
  may exist), dry-run simulates and never sends, live simulates then multi-path
  sends. Covered by tests.
- Tests: broadcaster gating (paper/dry-run/live/sim-fail), wallet
  (load/require-live/ephemeral/whitelist), RPC semaphore concurrency bound.
  75 tests total.

**Deliverable (partial):** execution safety foundation. Remaining for Phase 4b:
the verified PumpSwap `buy`/`sell` instruction builder + pre-signed exit ladder +
dry-run swap simulation + H4 sellability — needs the full pump_amm IDL address
constants and a wallet for realistic dry-run verification.

## Phase 3 — Pricing + paper positions

- `positions/pricing.ts` — local price from pool vault reserves (never an
  external price API, Section 7.2) + `PricePoller` that polls all open positions'
  vaults in one batched call per tick (free tier; gRPC per-slot later).
- `exits/engine.ts` — pure `evaluateExit` covering TP1 (partial), TP2, trailing
  (arms +25%, tightens on high-vol), hard stop, and the aggressive time stop.
- `positions/position.ts` — `PaperPosition` FSM (OPEN → EXITING → CLOSED) with
  partial TP1, the raised post-TP1 stop, high-water tracking, and staged
  realized PnL.
- `positions/manager.ts` — opens a paper position per accepted candidate,
  drives the FSM off price ticks, enforces a max-concurrent cap, applies a
  configurable fee model (swap fee per leg + priority/tip per tx), and persists
  fee-adjusted PnL.
- New bus event `openPosition`; pipeline emits it on accept (with pool pricing
  refs). Config: `positions.pricePollMs`, `fees.*`, `exits.tp1MoveStopToPct`.
- Wired into bootstrap; graceful shutdown of poller + manager.
- Tests: computePrice, all exit triggers, position lifecycle (TP1→TP2, hard
  stop, raised stop, trailing, time stop), and a manager integration test
  (open → staged exit → net PnL, plus the concurrency cap). 65 tests total.

**Deliverable:** local pricing + full exit-trigger FSM running against paper
fills, feeding the 7-day soak. (Full risk manager — daily loss, consecutive
losses, kill switch — is Phase 5; a minimal concurrency cap is in place now.)

## Phase 2b — Verified PumpSwap pool decoder

- `enrichment/pool.ts` — PumpSwap `Pool` account decoder. Field offsets from the
  official pump-amm IDL, **verified live** against 3 real pools before use
  (base_mint@43 == token, quote_mint@75 == WSOL, vaults returned sane reserves).
  Pool discovery via `getProgramAccounts` memcmp on base_mint (PumpPortal only
  gives a `"pump-amm"` label). Fetches reserves (vault balances) + lp_mint supply.
- `core/rpc.ts` — added `getProgramAccountsBase64`.
- Enrichment now fetches the pool in parallel under the same budget.
- Guardrail checks promoted from `unknown` to **live**:
  - **H3 LP burned** — lp_mint circulating supply == 0 (verified: canonical
    migrations burn LP to 0; non-zero == withdrawable → fail).
  - **H5 concentration** — top-10 / single-holder caps, excluding the decoded
    pool vaults and burn addresses.
  - **H6 creator holdings** — dev (coin_creator) holdings vs cap.
  - **H7 liquidity floor + impact** — SOL reserve floor and constant-product buy
    impact (dy / quoteReserve).
  - **H8** — now also checks the identified creator against the blacklist.
- `checks/pending.ts` trimmed to **H4** only (sellability — needs Phase 4 swap-sim).
- Tests: pool decode (valid + rejects wrong owner/disc/non-WSOL), token-account
  amount, and H3/H5/H6/H7 logic. 46 tests total.

**Deliverable:** 9 of 10 hard-fail checks enforced live on the free tier; only
H4 (honeypot simulation) remains, pending the Phase 4 swap builder.

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
