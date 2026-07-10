# Changelog

## Reliable rejected-coin recovery

- Split H4 unknowns into transaction-size, account-setup, wallet, RPC, and
  not-run causes. Added a guarded relaxed-risk lane for the first two only when
  every other hard check passes; live config caps it at 0.02 SOL and one open
  relaxed position through the existing controls.
- Added wallet and ATA preflight to the atomic buy/sell probe, including
  idempotent ATA setup and persisted transaction-size / lookup-table telemetry.
- Versioned new shadow outcomes as `exit_fsm_v1`, excluded legacy peak-only rows
  from PnL denominators, and added per-code / code-combination cohorts.
- Added durable shadow coverage counters plus enriched detail and summary CSVs
  for threshold replay.

## Dashboard: veto dry-run performance panel + CSV download

- Operator UI panel **Veto dry-run performance** shows live accepted snapshot
  vs per-veto-reason counterfactual dry-run stats (win%, expectancy, net PnL,
  MFE, hit50, hold) with range control (24h / 7d / 30d / all).
- Downloads: `GET /api/reports/veto-dry-run-summary.csv` (by reason + live
  headline) and `GET /api/reports/veto-dry-run.csv` (mint-level blotter from
  `shadow_outcomes` only — never mixed with live positions).
- Dashboard dist rebuilt for static serve.

## Dual-track veto dry-run performance

- Extended the shadow tracker so vetoed candidates get a capital-free paper
  position at graduation baseline and are driven through the same exit FSM
  (TPs / trail / stops / time stop) + fee drag used for non-live accounting,
  until close. Outcomes store realized-style net PnL, MFE/MAE, hold time, exit
  reason, and primary (plus full) veto codes in `shadow_outcomes` — never in
  `positions`, so live capital PnL stays clean.
- Peak hit-rate metrics (`hit_25` / `hit_50`) remain as a complement.
- Shadow concurrency (`shadow.maxConcurrent`) stays independent of live
  `risk.maxConcurrentPositions` and never sends or broadcasts transactions.
- Comparison analytics: `getVetoDryRunComparison` +
  `/api/analytics/veto-dry-run-comparison` and a strategy-report section put
  live accepted stats side-by-side with per-veto-reason dry-run performance.
- Shared `estimatePaperFees` used by position manager and shadow dry-run.
- Tests: full dry-run close with net PnL + veto reason, comparison aggregation
  separating live vs veto buckets, structural no-send on the veto path.

## Phase 6b — Durable live exit supervisor

- Added `positions.exit_intent_json` plus repository recovery for latest
  `EXITING` rows.
- Added a live-only `ExitSupervisor` that persists exit intent before broadcast,
  retries unresolved exits, escalates full-exit ladder slippage, reconciles
  wallet token balance after confirmed sells, and engages the internal kill
  switch when max attempts are exhausted while tokens remain.
- Reworked live `PositionManager` ticks so live mode previews exit triggers
  without mutating the paper FSM, then applies the fill only after supervisor
  confirmation and wallet-balance reconciliation.
- Startup now recovers unfinished `EXITING` rows before normal open-position
  recovery and before detection/guardrails can open new entries.
- Added exit retry config knobs: `exits.exitRetryMs`,
  `exits.maxExitAttempts`, and `exits.exitCriticalAlertEveryMs`.
- Tests: live manager verifies `EXITING` persistence before sell broadcast,
  unconfirmed exits stay unresolved with kill switch engaged, and malformed exit
  recovery metadata blocks live entries.

## Phase 6 — Live execution reconciliation + Jito

- Reworked live position entry so `OPEN` is only persisted after the buy
  broadcasts, confirms, and the wallet's actual base-token balance reconciles.
  Failed or unconfirmed entries persist `FAILED` and do not register price
  polling.
- Added Jito primary broadcast support with base64 `sendTransaction`, optional
  `x-jito-auth`, cached tip accounts, dynamic tip-floor sizing, and normal RPC
  fallback. Broadcast results now distinguish sent vs confirmed and persist
  route/attempt metadata.
- Wired actual raw token balances into live sell sizing. TP1 partial exits build
  fresh sells; full-remainder exits use the pre-signed slippage ladder and fall
  back to a fresh worst-tier sell when stale or unavailable.
- Added live crash recovery: startup restores persisted open positions from
  pricing metadata + wallet balance, rebuilds ladders, or engages the kill
  switch when recovery is ambiguous.
- Added execution metadata persistence (`execution_json`) and config/env/docs
  updates for Jito tips and the live gate checklist.
- Tests: Jito sender, dynamic fee/tip planner, confirmed broadcaster behavior,
  and live position manager entry/sell reconciliation. 144 tests total.

## Phase 5 — Pre-live safety systems (Steps 0–5)

Closing the pre-live gap audit. All additive + dependency-injected; 135 tests.

- **Step 0** — `rpc.ts`: getBalance / getSignatureStatuses / getTokenAccountBalance
  (+ executable on getMultipleAccounts). `db.ts`: idempotent `positions` column
  migration (raw_base_amount, pricing_json). `repositories.ts`: insertPriceTick,
  sumRealizedPnlSince, countClosedByTriggerSince, recentClosedPnls,
  latestOpenPositions.
- **Step 1** — `core/programs.ts` assertProgramsExist() enforced at boot (the
  inert `assertProgramIdsOnChain` flag now does something). Price ticks persisted
  from PricePoller.
- **Step 2** — `risk/manager.ts`: circuit breakers (daily loss w/ UTC reset,
  consecutive-loss timed halt, 24h emergency-exit count, wallet floor,
  stream-down, kill latch); emits `breaker` events + persists them; rehydrates
  counters from the DB on start. Wired into H10 + defense-in-depth in the manager
  + per-screening wallet-balance refresh.
- **Step 3** — kill switch: `risk/killswitch.ts` KILL-file watcher + Telegram
  `/kill` + `/status` (admin-gated, long polling) + `PositionManager.forceCloseAll`
  (finally wires `PaperPosition.forceClose`).
- **Step 4** — `positions/monitors.ts` EmergencyMonitor (LP-pull vs rolling
  reserve high; creator-dump vs first-seen balance) piggybacked on the price
  poller (creator ATA batched into the same getMultipleAccounts) → EMERGENCY_EXIT
  + auto-blacklist mint+creator. Feeds the risk manager's emergency-count breaker.
- **Step 5** — `detector/heliusWs.ts` Helius WebSocket feed (logsSubscribe on the
  pump.fun program, wss derived from the existing key; mint recovered
  index-independently from tx token balances). Verified live: subscribes and
  detects migrations. NOTE: it's a **redundancy** feed, not a latency win —
  logsSubscribe requires a getTransaction round-trip to resolve the mint, so
  PumpPortal usually wins the emission race. (Helius `transactionSubscribe` needs
  the separate Atlas endpoint; the standard endpoint accepts-but-never-delivers.)

**Remaining before live:** Step 6 (live fill reconciliation + ladder integration
— money-critical), Step 7 (crash recovery), Step 8 (pilot config).

## Phase 4d — Exit ladder + H4 sellability

- `positions/presign.ts` — pre-signed exit ladder (Section 7.2): builds + signs a
  full-exit sell at each slippage tier (default 2/5/10/25%), refreshes with a
  fresh blockhash, and selects a tier by target slippage (emergency → worst,
  escalation → next-looser). Config: `exits.ladderSlippageTiers`. Verified live:
  4 tiers built, fresh blockhash on refresh.
- `executor/sellability.ts` — H4 honeypot probe: an ATOMIC buy+sell simulation
  (buy provides the tokens, sell in the same tx proves they're sellable). Needs
  a funded wallet; classifies unfunded/ambiguous errors as `unknown`, never a
  false `fail`. Verified live: returns `unknown` on the unfunded wallet.
- Wired H4 into screening — the probe runs in the pipeline (dry-run/live), sets
  `enrichment.sellable`, and `checkSellability` reads it. **All 10 hard-fail
  checks are now wired** (H4 conclusive only with a funded wallet).
- Tests: ladder (refresh, tier selection, staleness) + H4 check states. 97 total.

**Remaining for Phase 4:** integrate the ladder into the live exit hot-path
(refresh on open + timer, dispatch pre-signed tx on trigger) — a live-latency
optimization best verified with a funded wallet.

## Phase 4c — Execution path + dry-run wiring

- `executor/sender.ts` — `RpcTxSender`: concrete broadcaster send path over a
  web3.js Connection (simulate + skip-preflight send).
- `executor/assemble.ts` — signs a v0 transaction from swap ixs: compute-unit
  limit + priority fee + optional Jito tip, with the wallet whitelist policy
  enforced on every instruction before signing.
- `executor/index.ts` — `Executor`: builds a swap (SDK) → assembles + signs →
  mode-gated broadcaster. `buy`/`sell` take pool address + base mint (the SDK's
  `swapSolanaState` fetches the rest).
- Wired into `PositionManager`: in dry-run/live it builds + broadcasts the real
  buy on open and sell on each exit fill, alongside the paper-accounting FSM.
  Constructed only when mode != paper; paper never touches the wallet/tx path.
- Generated a dedicated bot wallet (secret in `.env`, gitignored/redacted).
- Verified live: full dry-run buy transcript through build → sign → simulate →
  **broadcaster gate held (simulated, not sent)**; dry-run bot boots with the
  executor ready. (A clean passing simulation needs ~0.05 SOL funded on the
  wallet — pump pools are mainnet-only.)

**Remaining for Phase 4:** pre-signed exit ladder (live sub-second latency) +
H4 sellability sim (atomic buy+sell simulation — needs a funded wallet). Both
are gated on funding / approaching the live pilot.

## Phase 4b — Swap construction (blocker found, then resolved via SDK)

Investigation (kept for the record): hand-rolled a PumpSwap buy/sell builder and
verified all 8 PDA seeds + the buy account list against real on-chain txs — but
discovered the deployed program requires 2-3 `remaining_accounts` absent from
BOTH the public and on-chain Anchor IDLs, varying per tx. Guessing those in
fund-handling code was rejected.

Resolution:

- Adopted `@pump-fun/pump-swap-sdk` for swap instruction construction — it
  encodes the remaining-accounts logic the IDLs omit. **Verified live**: emits a
  26-account buy / 24-account sell with correct discriminators, touching only
  whitelisted programs.
- `executor/pumpAmm.ts` — thin SDK wrapper (`buildBuy`/`buildSell` via
  `OnlinePumpAmmSdk.swapSolanaState` + `buyQuoteInput`/`sellBaseInput`), with a
  whitelist guard on every emitted instruction (Section 8). Removed the
  incomplete hand-rolled builder + its tests.
- `core/constants.ts` — whitelisted the ATA program + pump fee program (touched
  by SDK swaps).
- Everything else stays on our own verified code (pool decode, pricing,
  guardrails); the SDK is used only to assemble the swap. Added `bn.js`.

**Remaining for Phase 4:** buy-tx assembly (compute budget + priority fee + SDK
ixs + Jito tip → sign → broadcaster) → pre-signed exit ladder → H4 sellability
sim → wire dry-run into the position lifecycle. 75 tests total.

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
