import { z } from 'zod';
import { PUMP_FUN_MIGRATION_AUTHORITY } from '../core/constants.ts';

/**
 * Config schema (Section 9). Validated with zod at startup; hot-reload is NOT
 * supported in v1 — restart to apply.
 *
 * Secrets never live here. Fields that reference secrets hold the *name* of an
 * env var (e.g. `keypairEnvVar`), and endpoint URLs may use `${ENV_VAR}`
 * interpolation resolved in load.ts.
 */

export const RunMode = z.enum(['paper', 'dry-run', 'live']);
export type RunMode = z.infer<typeof RunMode>;

const pct = z.number().min(0).max(100);
const positive = z.number().positive();
const nonNeg = z.number().nonnegative();

const WalletConfig = z
  .object({
    keypairEnvVar: z.string().min(1).default('WALLET_PRIVATE_KEY'),
    balanceFloorSol: nonNeg.default(0.1),
    // Live only: close empty token accounts (rent ~0.00204 SOL each, left
    // behind by every buy) at boot and every N minutes, returning the rent to
    // the wallet. Off the exit hot path by design. 0 = never sweep.
    sweepEmptyAtasMinutes: nonNeg.default(10),
  })
  .strict();

const RpcConfig = z
  .object({
    // Helius mainnet HTTP endpoint (key in URL, `?api-key=`). Used for
    // on-chain confirmation, enrichment, and execution. The WS feed derives
    // its wss:// endpoint from this automatically — no separate var needed.
    primaryHttp: z.string().min(1),
    // Helius LaserStream gRPC — the low-latency detection
    // upgrade, covered on Business+ plans. Blank-safe: an unfilled
    // ${HELIUS_GRPC_URL} slot resolves to "" and is dropped (not rejected),
    // so this stays wired in config.yaml and activates the moment the plan
    // covers mainnet gRPC — no config edit, just fill .env and restart.
    // The gRPC/LaserStream feeds self-skip while this is unset.
    primaryGrpc: z
      .string()
      .optional()
      .transform((v) => {
        const trimmed = v?.trim();
        return trimmed ? trimmed : undefined;
      }),
    // Helius API key for the gRPC endpoint (usually the same key as HTTP).
    // Needed only when primaryGrpc is set.
    primaryGrpcTokenEnvVar: z.string().optional(),
    // Second independent provider for redundant broadcast (Phase 4 / live).
    // NOTE: broadcast only. To use it for READS too, list it in fallbackHttp.
    // Blank-safe: an unfilled ${SECONDARY_HTTP_URL} interpolates to "" and is
    // treated as unset rather than failing the boot.
    secondaryHttp: z
      .string()
      .optional()
      .transform((u) => (u && u.trim().length > 0 ? u.trim() : undefined)),
    // Dedicated READ endpoint for enrichment (pool GPA, holders, DAS getAsset,
    // background wallet poll). When set, a second RpcClient is built with this as
    // primary. Helius serves DAS + indexed GPA on the primary itself, so this
    // normally points at the same Helius URL — it exists so reads stay on
    // Helius even if fallbackHttp[0] ever points elsewhere. When omitted,
    // index.ts uses fallbackHttp[0] or primaryHttp.
    enrichmentHttp: z.string().min(1).optional(),
    // Independent READ endpoints, tried in order when the primary is
    // rate-limited or down. Reads are idempotent, so failover is safe.
    //
    // Strongly recommended in live mode: with a single endpoint, an exhausted
    // plan makes every enrichment field `unknown`, and in live mode unknowns
    // are vetoes — so a dead RPC turns into a silent 100% veto rate rather than
    // a visible outage.
    //
    // Blank entries are dropped, NOT rejected. config.yaml lists ${ENV_VAR}
    // slots for providers you may not have signed up for yet; an unfilled slot
    // interpolates to "" and must be ignored rather than crash the boot.
    fallbackHttp: z
      .array(z.string())
      .default([])
      .transform((urls) => urls.map((u) => u.trim()).filter((u) => u.length > 0)),
    pumpportalWs: z.string().url().default('wss://pumpportal.fun/api/data'),
    // Cap concurrent in-flight RPC requests to stay under free-tier rate limits.
    maxConcurrentRequests: z.number().int().positive().default(4),
    // Per-attempt timeout for a single RPC call before RpcClient aborts and
    // fails over. Must stay well under guardrails.enrichmentBudgetMs — the old
    // hardcoded 5000ms default was ~2x the 2500ms enrichment budget, so ANY
    // stalled call (not just a fast 429) guaranteed the whole enrichment field
    // timed out to `unknown` before a fallback endpoint was ever tried.
    readTimeoutMs: z.number().int().positive().default(900),
  })
  .strict();

const JitoConfig = z
  .object({
    blockEngineUrl: z.string().min(1),
    authTokenEnvVar: z.string().optional(),
    tipCapLamports: z.number().int().positive().default(2_000_000),
    tipFloorUrl: z.string().url().default('https://bundles.jito.wtf/api/v1/bundles/tip_floor'),
    tipRefreshMs: z.number().int().positive().default(10_000),
    minTipLamports: z.number().int().positive().default(1_000),
    fallbackTipLamports: z.number().int().positive().default(10_000),
  })
  .strict();

const DetectorLivenessConfig = z
  .object({
    // Slot-subscribed feeds (helius-ws, laserstream) tick every ~400 ms; no
    // frame of any kind for this long → forced reconnect.
    slotSilenceMs: z.number().int().positive().default(5_000),
    // PumpPortal has no heartbeat: absolute-silence bound (migrations occur
    // roughly every 1–3 minutes, so 10 min of silence is a strong signal).
    portalSilenceMs: z.number().int().positive().default(600_000),
    // PumpPortal missed this many consecutive graduations that an on-chain
    // feed delivered → forced reconnect.
    portalMissedGraduations: z.number().int().positive().default(3),
    // Tripwire: PumpPortal delivered this many consecutive graduations that NO
    // healthy on-chain feed saw → alert (detector.migrationAuthority probably
    // rotated). Alert only, never a reconnect.
    onChainMissedGraduations: z.number().int().positive().default(3),
  })
  .strict();

const DetectorConfig = z
  .object({
    // PumpPortal WebSocket — free, purpose-built migration events. Default feed.
    pumpportalEnabled: z.boolean().default(true),
    // PumpPortal new-token creation events on the same socket (pre-graduation
    // lane, S0 observe-only: persisted to `launches`, never screened/traded).
    pumpportalNewTokenEnabled: z.boolean().default(true),
    // Official Helius LaserStream SDK — low-latency detection via gRPC with
    // automatic reconnect + slot replay. Opt-in drop-in upgrade, paid tier.
    laserstreamEnabled: z.boolean().default(false),
    // Helius WebSocket (logsSubscribe on the pump.fun program) — direct on-chain
    // feed using the existing Helius key (wss derived from rpc.primaryHttp).
    heliusWsEnabled: z.boolean().default(false),
    // Atlas `transactionSubscribe` first (needs a Developer+ plan), with
    // automatic fallback to logsSubscribe when the server rejects it. The
    // notification carries logs + token balances, so the mint resolves with
    // no getTransaction round trip.
    heliusAtlasEnabled: z.boolean().default(false),
    // Cross-feed dedupe window by mint (Section 4.2).
    dedupeTtlMs: z.number().int().positive().default(5 * 60_000),
    // Verify the migration tx landed on-chain (no error) before emitting a
    // graduation. Requires rpc.primaryHttp; skipped (with a warning) if absent.
    confirmOnChain: z.boolean().default(true),
    // Reconnect backoff bounds for WS/gRPC feeds.
    reconnectBaseMs: z.number().int().positive().default(500),
    reconnectMaxMs: z.number().int().positive().default(30_000),
    // How often to log rolling detection-latency stats.
    latencyLogEveryN: z.number().int().positive().default(10),
    // pump.fun migration authority: every Migrate/MigrateV2 tx includes it, so
    // the on-chain feeds subscribe with accountRequired [pumpFun, authority]
    // and receive migrations only instead of the whole pump.fun firehose.
    // '' disables the narrowing (devnet uses a different authority).
    migrationAuthority: z.string().default(PUMP_FUN_MIGRATION_AUTHORITY),
    // Feed-liveness watchdog. Health used to flip only on socket close, so a
    // half-open connection went unnoticed for 35 minutes.
    liveness: DetectorLivenessConfig.default({}),
    // Drop a detection whose migration slot is more than this many slots
    // behind the SlotClock (LaserStream `replay: true` can re-deliver old
    // migrations after a reconnect). 0 = off.
    maxStaleSlots: z.number().int().nonnegative().default(150),
  })
  .strict();

const EntryConfig = z
  .object({
    // Position size as a % of the in-memory wallet SOL cache at entry time.
    // The cache is primed at boot and background-polled — send does not wait
    // on getBalance.
    minSizeWalletPct: z.coerce.number().min(0).max(100).default(5),
    baseSizeWalletPct: z.coerce.number().min(0).max(100).default(8),
    maxSizeWalletPct: z.coerce.number().min(0).max(100).default(10),
    // Absolute dust floor in SOL. Percent-of-wallet is never allowed to
    // shrink a trade below this (PumpSwap + Jito tip + ATA rent). Also the
    // size used when no wallet balance is available (paper tests).
    minAbsoluteSol: positive.default(0.01),
    maxSlippagePct: pct.default(5),
    // Looser bounds the live BUY retries with after an ExceededSlippage (6004)
    // simulate, in order; empty = no retry. Decoupled from the EXIT ladder:
    // 2026-09-16 live entries reused exits.ladderSlippageTiers (10, 25) and
    // either failed all three simulates or filled up to 25% above the quote,
    // right at the top of the sniper spike — those became the 3–15 s stops.
    // If a fresh graduation does not fill within ~8% the trade is already gone.
    buyRetrySlippageTiers: z.array(pct).default([8]),
    /**
     * Skip the live buy when the pool mid has moved more than this % between
     * the verdict's pool snapshot and the buy quote's own state read (the
     * sniper window). The move is recorded on every entry either way
     * (execution_json.entry.entryMovePct). Absent = record only.
     */
    maxEntryMovePct: positive.optional(),
    minEntryScore: z.number().min(0).max(100).default(60),
    // Scale size by the soft-score multiplier (work plan 2026-09-25 P2.5, F2:
    // the score is flat — 421/524 trades at exactly 85 — so it sized on
    // noise). false = base rung x momentum; minEntryScore still gates.
    scoreSizingEnabled: z.boolean().default(true),
  })
  .strict()
  .refine((e) => e.maxSizeWalletPct >= e.baseSizeWalletPct, {
    message: 'entry.maxSizeWalletPct must be >= entry.baseSizeWalletPct',
  })
  .refine((e) => e.minSizeWalletPct <= e.maxSizeWalletPct, {
    message: 'entry.minSizeWalletPct must be <= entry.maxSizeWalletPct',
  })
  .refine((e) => e.minSizeWalletPct <= e.baseSizeWalletPct, {
    message: 'entry.minSizeWalletPct must be <= entry.baseSizeWalletPct',
  });

const GuardrailsConfig = z
  .object({
    top10HolderCapPct: pct.default(25),
    singleHolderCapPct: pct.default(8),
    creatorHoldingsCapPct: pct.default(5),
    minPoolSol: nonNeg.default(25),
    maxBuyImpactPct: pct.default(3),
    creatorMaxLaunches7d: z.number().int().nonnegative().default(3),
    // Narrow H4 bypass: only the known atomic-probe transaction-size failure may
    // be tolerated, and only when every other hard check is clean.
    tolerateTxTooLargeSellability: z.boolean().default(false),
    // Optional relaxed-risk lane for structural account-setup limitations in
    // the atomic probe. Wallet/RPC/not-run failures are never tolerated.
    tolerateInconclusiveSellability: z.boolean().default(false),
    sellabilityLookupTableAddress: z.string().min(1).optional(),
    /**
     * Slippage bound (%) of the H4 probe's buy leg. Only decides whether the
     * simulation reaches the sell leg — a honeypot fails the sell at any bound.
     * 15 made the buy ix fail ExceededSlippage on ~95% of real (sniped)
     * graduations, so H4 was a volatility veto in disguise (2026-09-18).
     */
    sellabilityProbeSlippagePct: positive.default(50),
    /**
     * Optional explicit early-move gate: veto (H4 unknown/price_moved, never
     * tolerated) when the pool's quote reserve moved more than this % between
     * the enrichment snapshot and the probe's state read. Absent = off; the
     * move is still recorded on enrichment.sellable.poolMovePct.
     */
    maxProbeMovePct: positive.optional(),
    // When the atomic buy+sell probe overflows the 1232-byte transaction limit
    // (the dominant H4 "unknown" cause on pools with large account sets), fall
    // back to simulating the BUY leg alone. A clean buy proves the pool is real
    // and buyable and the account setup works; the sell-block honeypot vectors
    // are already covered on-chain by H2 (freeze) and H9 (Token-2022 traps), so
    // this is admitted only as a relaxed-risk accept and only when every other
    // hard check — H2 and H9 included — passes. Off by default (conservative);
    // the live config opts in. Independent of tolerateTxTooLargeSellability,
    // which blindly tolerates the overflow without any buy-leg evidence.
    sellabilityBuyOnlyBackstop: z.boolean().default(false),
    // Volume-for-risk trade, not an infra fix: tolerates H4 `rpc_unavailable`
    // and `not_run` — the atomic buy+sell probe never ran at all (RPC down, no
    // funded wallet reachable, etc.), so acceptance falls back to trusting H2
    // (freeze authority) + H9 (Token-2022 traps) alone, with NO dynamic sell
    // confirmation for this candidate. A transfer-tax/honeypot trap that only
    // shows up in a live sell simulation — not in static extensions — would go
    // undetected here. Admitted only as a relaxed-risk accept (same size caps
    // below), and only when every other hard check is an explicit pass.
    // `price_moved` is never eligible for this or any other flag (see
    // canTolerateUnknown). Off by default.
    tolerateUnprobedSellability: z.boolean().default(false),
    // General relief valve for H1/H2/H3/H5/H6/H9 unknowns (mint/pool/holders
    // account reads unavailable — never a signal in themselves, unlike H4's
    // reasons which include real signals like price_moved). 2026-09-17: 98.1%
    // of live vetoes had >=1 unknown check and only ~6% were a genuine hard
    // fail, so an RPC data gap — not real risk — was the dominant blocker, and
    // it defeated even H4's own tolerance flags above (which require every
    // OTHER check to be an explicit pass, so a co-occurring H1/H5 unknown
    // blocked the rescue as hard as a real fail would). Still refuses outright
    // the moment anything is an explicit fail; an accepted candidate is sized
    // down via relaxedRisk same as every other relaxed-entry path. Off by
    // default (conservative); the live config opts in.
    tolerateUnknownWhenNoHardFail: z.boolean().default(false),
    // Strict baselines used to tag "relaxed" accepts when config thresholds are
    // widened. Defaults match the researched v1 guardrail thresholds.
    strictTop10HolderCapPct: pct.default(25),
    strictCreatorHoldingsCapPct: pct.default(5),
    strictMinPoolSol: nonNeg.default(25),
    // Master switch for relaxed-risk ACCEPTS (work plan 2026-09-25 P2.1, F9):
    // relaxed=1 trades ran WR 40.4 % / −6.97 %/trade vs strict 50.3 % /
    // −1.97 %. false turns every would-be relaxed accept into the veto
    // RELAXED_DISABLED — still tagged, still shadow-tracked for re-evaluation.
    relaxedRiskEnabled: z.boolean().default(true),
    relaxedRiskMaxReasons: z.number().int().positive().default(1),
    relaxedRiskSizeMultiplierCap: positive.default(0.5),
    // Cap for relaxed-risk accepts as a % of wallet (replaces the old 0.02 SOL
    // absolute). Also floored at entry.minAbsoluteSol at open time.
    relaxedRiskMaxSizeWalletPct: pct.default(3),
    relaxedRiskMaxOpenPositions: z.number().int().positive().default(1),
    relaxedRiskTimeStopMinutes: positive.default(10),
    relaxedRiskTrailingGapPct: positive.default(10),
    relaxedRiskEmergencyLpDropPct: pct.default(10),
    relaxedRiskTp0Enabled: z.boolean().default(true),
    // H12 canonical-graduation population filter (work plan 2026-09-25 P2.2,
    // F10; veto-review segment A). See guardrails/checks/population.ts.
    population: z
      .object({
        enabled: z.boolean().default(false),
        requirePumpSuffix: z.boolean().default(true),
        minMintAgeMs: z.number().int().nonnegative().default(30_000),
        minPoolSol: nonNeg.default(60),
        maxPoolSol: positive.default(90),
        unknownAgePolicy: z.enum(['veto', 'allow']).default('veto'),
      })
      .strict()
      .default({}),
    // Global enrichment budget; anything slower is marked "unknown" (Section 5 / 6.3).
    enrichmentBudgetMs: z.number().int().positive().default(1500),
    // Local retry schedule (ms between attempts) for getTokenLargestAccounts
    // returning -32602 "not a Token mint" — the RPC token index lags a brand
    // new mint by a few seconds after migration. Must sum to less than
    // enrichmentBudgetMs (a retry that cannot finish inside the budget only
    // turns into a budget timeout). The default fits the 1500 ms default
    // budget; config.yaml pairs a 2500 ms budget with [0, 300, 700, 1200].
    holdersNotMintRetryDelaysMs: z.array(z.number().int().nonnegative()).default([0, 300, 600]),
    // RugCheck advisory soft signal (Section 6.2). Off by default; the API key
    // (higher rate limits) is read from this env var when present.
    rugcheckEnabled: z.boolean().default(false),
    rugcheckApiKeyEnvVar: z.string().default('RUGCHECK_API_KEY'),
    // Coin-age advisory soft signal (Section 6.2 / Section 13: third-party API,
    // never a hard-fail input — see src/enrichment/tokenAge.ts). A "graduation"
    // for a mint created long before detection is a red flag (stale/misattributed
    // detection, not fresh momentum); penalizes score rather than vetoing.
    // No API key needed (pump.fun's own public coin endpoint).
    tokenAgeEnabled: z.boolean().default(true),
    // Mint age (minutes) at/under which there is no penalty.
    tokenAgeFreshMinutes: positive.default(60),
    // Mint age (minutes) at/beyond which the max penalty applies (linear ramp
    // from tokenAgeFreshMinutes). 24h: most genuine graduations happen well
    // inside a day of creation; tune from real creation-vs-graduation data
    // once there's enough live history (see rug forensics, LIVE_PILOT_PLAN §4 S3).
    tokenAgeStaleMinutes: positive.default(24 * 60),
    // Score points subtracted at/beyond tokenAgeStaleMinutes.
    tokenAgeMaxPenalty: nonNeg.default(20),
    // --- Early-flow momentum soft signal (Section 6.2) ---
    // Window to observe net SOL inflow after graduation, ms. Delays entry by this
    // much, so kept short; 0 disables sampling entirely.
    momentumWindowMs: z.number().int().nonnegative().default(1000),
    // Optional per-graduation A/B buckets for the early-flow window. When this
    // array is non-empty, enrichment randomly selects one bucket per candidate.
    momentumWindowBucketsMs: z.array(z.number().int().nonnegative()).default([0, 250, 500, 750, 1000]),
    // Net SOL inflow over the window at/above which the full momentum bonus is
    // awarded (linear, and symmetric for net outflow → penalty).
    momentumStrongInflowSol: positive.default(10),
    // Max soft-score points the momentum signal may add (or subtract).
    momentumMaxScoreBonus: nonNeg.default(15),
    // Inflow rate (SOL/sec) at/above which the candidate is flagged high-volatility,
    // tightening the trailing stop (exits.trailingGapHighVolPct).
    highVolInflowRateSolPerSec: positive.default(2),
    // --- Momentum-driven position sizing (Round 3) ---
    // early_flow (net SOL inflow in the first moment post-graduation) is the ONE
    // feature that separates winners from craters (~0.44 vs ~0.03 SOL in the
    // shadow data). Rather than gate on it (which would cut volume), scale SIZE
    // by it: strong inflow → full size, ~0 inflow → floor size. Keeps volume,
    // concentrates capital on higher-conviction entries.
    momentumSizeEnabled: z.boolean().default(true),
    // Net inflow (SOL) at/above which momentum gives the full size factor (1.0).
    // Calibrated to real inflows (~0.5 SOL), NOT the score's strongInflowSol (10).
    momentumSizeFullInflowSol: positive.default(0.5),
    // Size factor at zero/negative inflow (the minimum momentum-scaled size).
    momentumSizeFloorMultiplier: z.number().min(0).max(1).default(0.4),
  })
  .strict()
  .refine((g) => g.holdersNotMintRetryDelaysMs.reduce((a, b) => a + b, 0) < g.enrichmentBudgetMs, {
    message: 'guardrails.holdersNotMintRetryDelaysMs must sum to less than enrichmentBudgetMs',
    path: ['holdersNotMintRetryDelaysMs'],
  });

const ExitsConfig = z
  .object({
    // Optional early partial take-profit (below TP1) for tokens that peak and
    // roll over before +50%. Off by default; a soft signal, never a rug guard.
    tp0Enabled: z.boolean().default(false),
    tp0Pct: positive.default(30),
    tp0SellFraction: z.number().min(0).max(1).default(0.33),
    tp0MoveStopToPct: positive.default(10),
    tp1Pct: positive.default(50),
    tp1SellFraction: z.number().min(0).max(1).default(0.75),
    tp2Pct: positive.default(100),
    trailingArmPct: positive.default(25),
    trailingGapPct: positive.default(15),
    // Tighter trail when the soft-signal engine sets the high-volatility flag.
    trailingGapHighVolPct: positive.default(10),
    hardStopPct: positive.default(20),
    // After TP1, the remainder's stop moves up to this gain % (Section 7.3).
    tp1MoveStopToPct: positive.default(20),
    timeStopMinutes: positive.default(15),
    // Dead-money exit: close a position that has shown no follow-through —
    // no TP0/TP1 yet, peak gain below deadMoneyMaxMfePct — once it is
    // deadMoneyMinutes old, instead of holding it to the time stop. The 7-day
    // dry run had 51% of trades sit the full time stop for a +3.7% median, and
    // every rug sat flat (MFE ~+4%) for minutes before dying; both are this
    // pattern. Fires as TIME_STOP with reason 'dead money' so dashboards need
    // no new trigger. Off by default; enable in the twin first
    // (dryRunTwin.exitOverrides) and promote once it beats the baseline.
    deadMoneyEnabled: z.boolean().default(false),
    deadMoneyMinutes: positive.default(3),
    deadMoneyMaxMfePct: nonNeg.default(5),
    emergencyLpDropPct: pct.default(15),
    // Rolling window (in price-poll ticks) for the LP-pull high-water mark.
    lpDropWindowTicks: z.number().int().positive().default(5),
    // In-position dev-dump monitor: fire EMERGENCY_EXIT when the creator sells
    // at least this % of their observed base-token holdings.
    creatorDumpEnabled: z.boolean().default(true),
    creatorDumpThresholdPct: pct.default(50),
    // LARGE_SELL emergency (work plan 2026-09-25 P2.3): one tick-to-tick
    // quote-reserve drop >= this % of pool SOL exits before the rolling LP
    // window catches up. 0 disables.
    largeSellPoolPct: pct.default(0),
    // Ladder refresh cadence — blockhashes expire in ~60-90s (Section 7.2).
    ladderRefreshMs: z.number().int().positive().default(45_000),
    // Pre-signed exit ladder slippage tiers (%), worst-case last. Escalation
    // walks from tightest to loosest; emergency exits jump to the last tier.
    ladderSlippageTiers: z.array(pct).nonempty().default([2, 5, 10, 25]),
    // Emergency-only bound: EMERGENCY_EXIT / KILL_SWITCH sells, and the final
    // attempts of an ordinary exit that has already failed every ladder tier.
    // The dry run's rugs moved -90..-99% inside one tick — a 25% ceiling
    // cannot sell into that, and a sell that never lands is a position held
    // to zero. A slippage bound is a floor on proceeds, not a fill price, so
    // a healthy pool still fills at the current price; the cost of the loose
    // bound is sandwich exposure, which is why it never touches TP/trailing.
    emergencySlippagePct: pct.default(90),
    // Durable live-exit supervisor retry loop. Live positions remain EXITING
    // until wallet token balance reconciles after a confirmed sell.
    exitRetryMs: z.number().int().positive().default(1500),
    maxExitAttempts: z.number().int().positive().default(6),
    exitCriticalAlertEveryMs: z.number().int().positive().default(10_000),
    // Confirmation timing for EXIT sends specifically. Shorter than the generic
    // 12s/500ms so a non-landing exit escalates to a looser/faster tier after a
    // couple of missed slots instead of waiting 12s while price keeps falling.
    exitConfirmTimeoutMs: z.number().int().positive().default(2500),
    exitConfirmPollMs: z.number().int().positive().default(200),
    // Skip the pre-send simulate on pre-signed ladder dispatches (already
    // validated at build). Saves an RPC round-trip on the exit hot path. Default
    // off — enable after a dry-run smoke test confirms the ladder path is clean.
    skipSimulateOnPresignedExit: z.boolean().default(false),
  })
  .strict();

const PositionsConfig = z
  .object({
    // Local price poll cadence per open position (free tier; gRPC gives per-slot).
    pricePollMs: z.number().int().positive().default(1000),
    // LaserStream account-subscribe on every tracked pool's vaults (+ creator
    // ATA) as a PUSH tick source (Business+ plan: mainnet gRPC). Additive to
    // the poller, which stays running for liveness (time-stops fire on
    // unchanged prices). Ticks reach the live manager, the twin and shadow
    // through the same handler as poll ticks. Self-disabling while
    // rpc.primaryGrpc is blank.
    laserstreamTicksEnabled: z.boolean().default(false),
    // Coalesce push ticks per pool: a hot pool can change every transaction,
    // and each tick is an FSM pass + a price_ticks row. 0 = no coalescing.
    laserstreamTickMinIntervalMs: z.number().int().nonnegative().default(100),
    // Tear down and reconnect the push stream when it is tracking pools but has
    // delivered no tick for this long (the SDK's own reconnect can come back
    // without our account filter, which is silent tick loss on the redundant path).
    laserstreamStaleTickMs: z.number().int().positive().default(30_000),
    /**
     * Commitment for vault price reads. Deliberately SEPARATE from
     * execution.stateCommitment so pricing can be rolled back on its own: a
     * stale 'confirmed' read is harmless for a quote (6004 -> retry) but is
     * total data loss for a tick, while a torn 'processed' read is harmless for
     * a quote (simulate catches it) and is handled for ticks by the suspect-tick
     * guard in PositionManager.onTick.
     *
     * Was effectively 'confirmed' (the RpcClient default) until 2026-09-22: a
     * pool created 1-2 slots earlier has vault accounts that are not yet visible
     * at 'confirmed', the poller emitted NO tick, and the exit FSM ran blind.
     * 14 of 25 live positions exited on a single price observation; all 14 lost,
     * median -22.6%, and live never once reached TAKE_PROFIT_1.
     */
    priceCommitment: z.enum(['processed', 'confirmed', 'finalized']).default('processed'),
    // Max pubkeys per getMultipleAccounts price read. Solana's server-side cap
    // is 100; above it the single batch failed WHOLESALE for every position.
    priceBatchSize: z.number().int().positive().max(100).default(100),
    // A poll cycle may never hold the in-flight guard longer than this. The
    // RpcClient's own timeoutMs does NOT bound it (Semaphore.acquire() sits
    // outside the AbortController), so without a deadline one queued read
    // starves every open position for the whole unbounded queue wait.
    pricePollDeadlineMs: z.number().int().positive().default(2_000),
    // Give the live price poller its own RpcClient so vault reads can never
    // queue behind an enrichment burst or the shadow tracker. TRADE-OFF: this
    // bypasses rpc.maxConcurrentRequests, spending ~2 RPS outside the global
    // budget (same call already made for dryRunTwin.dedicatedRpc).
    dedicatedPriceRpc: z.boolean().default(true),
    /**
     * Blind-position guard. Distinct from exits.timeStopMinutes (600_000 ms —
     * ~100x too slow for a failure mode that kills positions in 3-7 s).
     * Escalation: no usable tick by blindFirstTickMs -> force a direct
     * readOnce; still nothing by blindExitMs -> close at market as
     * NO_PRICE_DATA. Holding a position we cannot see is never correct.
     */
    blindGuardEnabled: z.boolean().default(true),
    // 3 missed cycles at pricePollMs 500 — fires on a fault, not on jitter.
    blindFirstTickMs: z.number().int().positive().default(1_500),
    // Lets the forced readOnce plus one failover hop land (rpc.readTimeoutMs
    // 900) while staying inside the observed 3-7 s death window.
    blindExitMs: z.number().int().positive().default(4_000),
    // A position that HAD ticks and then went quiet gets the same force-read,
    // but more rope: it has a real last-known price to fall back on.
    blindStaleTickMs: z.number().int().positive().default(6_000),
    // Blind exits are an infrastructure failure, not a trade outcome. N of them
    // means the bot is flying blind and must stop opening positions.
    blindExitKillSwitchCount: z.number().int().positive().default(3),
  })
  .strict();

/**
 * Shadow (counterfactual) dry-run of candidates we did NOT trade. Opens a paper
 * position at graduation baseline and drives the same exit FSM + fee drag used
 * for non-live accounting, so veto quality is measured as realized-style net
 * PnL / MFE / MAE / exit reason (not only peak hit rates). Never trades capital;
 * runs on its own slow poller with a hard concurrency cap so it can't compete
 * with live exit pricing or inflate live risk caps.
 */
const ShadowConfig = z
  .object({
    enabled: z.boolean().default(true),
    windowMinutes: positive.default(20),
    pollMs: z.number().int().positive().default(3000),
    maxConcurrent: z.number().int().positive().default(25),
    // Simulated entry size for fee-adjusted PnL. Defaults to entry.minAbsoluteSol
    // when omitted at wiring time (see index.ts).
    sizeSol: positive.optional(),
  })
  .strict();

/**
 * Capital-free paper tracking of pre-graduation launches (S1). One batched
 * account read per tick (~0.1 rps at defaults) against the shared RPC
 * budget — the cap and cadence are the RPC-cost controls. Never screens,
 * trades, or contacts risk.
 */
const LaunchTrackConfig = z
  .object({
    enabled: z.boolean().default(true),
    windowMinutes: positive.default(120),
    pollMs: z.number().int().positive().default(10_000),
    maxConcurrent: z.number().int().positive().default(10),
  })
  .strict();

/**
 * Venue-aware pre-graduation screening (S2, no capital). Dormant until S3
 * wires it to execution: `enabled` gates the screening→entry path that does
 * not exist yet. Thresholds are provisional pending S1 calibration —
 * minCompletionPct is the volume control (late-curve-only at ~17/min flow).
 */
const PregradConfig = z
  .object({
    enabled: z.boolean().default(false),
    minCompletionPct: z.number().min(0).max(100).default(80),
    minRealSol: z.number().nonnegative().default(50),
    maxTop10Pct: z.number().nonnegative().default(45),
    maxCreatorPct: z.number().nonnegative().default(8),
    allowUnindexed: z.boolean().default(false),
    // S3b live execution (dust-level until S1+S3 data says otherwise).
    buySol: positive.default(0.05),
    takeProfitPct: z.number().positive().default(30),
    stopLossPct: z.number().positive().default(15),
    trailPct: z.number().positive().default(10),
    timeStopMinutes: positive.default(30),
    maxConcurrent: z.number().int().positive().default(1),
    maxDailyLossSol: positive.default(0.1),
    maxConsecutiveLosses: z.number().int().positive().default(3),
    selectPollMs: z.number().int().positive().default(30_000),
    managePollMs: z.number().int().positive().default(5_000),
    slippagePct: z.number().nonnegative().default(5),
    priorityFeeSol: z.number().nonnegative().default(0.00001),
  })
  .strict();

/**
 * Subset of ExitsConfig the twin may override for a strategy experiment. Every
 * key optional with NO defaults, so an absent key means "same as live".
 */
const ExitOverridesConfig = z
  .object({
    deadMoneyEnabled: z.boolean().optional(),
    deadMoneyMinutes: positive.optional(),
    deadMoneyMaxMfePct: nonNeg.optional(),
    tp0Enabled: z.boolean().optional(),
    tp0Pct: positive.optional(),
    tp0SellFraction: z.number().min(0).max(1).optional(),
    tp0MoveStopToPct: positive.optional(),
    tp1Pct: positive.optional(),
    tp1SellFraction: z.number().min(0).max(1).optional(),
    tp2Pct: positive.optional(),
    trailingArmPct: positive.optional(),
    trailingGapPct: positive.optional(),
    trailingGapHighVolPct: positive.optional(),
    hardStopPct: positive.optional(),
    tp1MoveStopToPct: positive.optional(),
    timeStopMinutes: positive.optional(),
  })
  .strict();
export type ExitOverrides = z.infer<typeof ExitOverridesConfig>;

/**
 * Dry-run TWIN of every ACCEPTED candidate — distinct from `shadow`, which
 * tracks VETOED candidates. Opens an ideal paper position at the same pool mid
 * `openLive` prices from, drives an INDEPENDENT exit FSM, and never touches the
 * Broadcaster or the wallet. delta(live, dry) is therefore total execution drag
 * (latency + slippage + real fees) — the number that says whether fast
 * execution recovers the slow-exit bleed.
 *
 * Also covers accepts live never traded (risk-blocked, failed entry), whose
 * twin PnL is measured opportunity cost. Rows land in `dry_run_positions`,
 * never in `positions`, so simulated losses can never trip the live kill switch.
 */
const DryRunTwinConfig = z
  .object({
    enabled: z.boolean().default(true),
    // Poll cadence. MUST match positions.pricePollMs or the delta conflates
    // poller cadence with execution drag — a slower twin invents hold-time and
    // exit-price deltas that live never had. Defaults to positions.pricePollMs
    // when omitted (see index.ts); a mismatch is logged as a warning.
    pollMs: z.number().int().positive().optional(),
    // Bounded concurrency, independent of risk.maxConcurrentPositions. Excess
    // candidates are dropped and recorded, never silently skipped.
    maxConcurrent: z.number().int().positive().default(8),
    // Give the twin poller its OWN RpcClient instead of sharing the primary.
    //
    // Default false, and the default matters: a dedicated client does not share
    // the primary's concurrency semaphore, so it spends quota OUTSIDE
    // rpc.maxConcurrentRequests. On a rate-limited endpoint that can push live
    // exit reads into 429-and-retry — slowing the real exits this feature exists
    // to measure, which corrupts the measurement and costs real money.
    //
    // Sharing trades that away for queueing: a twin poll can briefly occupy a
    // slot a live exit read wants. Enable this only when RPC quota is ample.
    dedicatedRpc: z.boolean().default(false),
    windowMinutes: positive.default(20),
    // mirror: twin size = the live sizeSol from the openPosition event, so
    //   netPnlDelta is directly the SOL cost of execution (no rescaling).
    // fixed:  constant notional, for cross-week comparability.
    sizeMode: z.enum(['mirror', 'fixed']).default('mirror'),
    sizeSol: positive.optional(), // only meaningful when sizeMode = 'fixed'
    // Cover accepts the live leg refused (max-concurrent, breaker, kill switch).
    coverBlocked: z.boolean().default(true),
    // Cover accepts whose live entry failed or went unconfirmed.
    coverFailed: z.boolean().default(true),
    // EXPERIMENT LANE. Exit-rule overrides applied to the twin ONLY, so a
    // strategy variant (dead-money exit, a different TP ladder) can run against
    // the live baseline on the same candidates and price paths.
    //
    // While any override is set, Δ(live, dry) is strategy difference PLUS
    // execution drag, not execution drag alone — the tracker logs a warning at
    // start and every closed twin row carries the override set in
    // `exit_overrides_json` so the two regimes never mix in a report.
    exitOverrides: ExitOverridesConfig.optional(),
  })
  .strict();

/**
 * Fee estimates used for paper-mode PnL so the soak report reflects real drag
 * (Section 13: fees can consume a large share of a +50% move). Wired into the
 * real executor in Phase 4.
 */
const FeesConfig = z
  .object({
    // PumpSwap swap fee per leg (~0.25%).
    swapFeePct: nonNeg.default(0.25),
    // Rough priority fee per transaction, in SOL (paper / twin accounting).
    estPriorityTipSolPerTx: nonNeg.default(0.001),
    // Jito tip per transaction, in SOL, charged on top when bundles are in
    // use. Keep 0 while the `jito` block is commented out; re-enabling Jito is
    // then this one line plus the block, not a fee-model re-tune.
    jitoTipSolPerTx: nonNeg.default(0),
    // Charge constant-product price impact on paper / twin fills (buy impact at
    // entry from the pool reserves, sell impact on every exit from the tick's
    // reserves). Without it the twin fills at the mid and the live-vs-twin Δ
    // reads pure impact as "execution drag". Recorded separately as
    // slippage_sol; net PnL subtracts it.
    modelPaperSlippage: z.boolean().default(true),
    // Priority-fee floor/cap in micro-lamports per compute unit. The floor was
    // raised from the old hardcoded 50k (~0.0000125 SOL at 250k CU) because a
    // low-fee exit tx waits many slots for inclusion while a graduated token is
    // crashing — the #1 cause of stops realizing far worse than configured.
    priorityFloorMicroLamports: z.number().int().nonnegative().default(250_000),
    priorityCapMicroLamports: z.number().int().positive().default(5_000_000),
    // Prefer Helius getPriorityFeeEstimate (medium level) over the
    // getRecentPrioritizationFees p75. Best-effort: unavailable methods or
    // endpoints silently fall back to the p75 path. Set false to force p75.
    useHeliusFeeEstimate: z.boolean().default(true),
    // Cache the fee plan for this long. Safe to serve stale: the result is not
    // pool-specific, it is clamped to [floor, cap], and every failure path in
    // buildFeePlan already degrades to the floor. Removes one serial RPC hop
    // from the pre-send path on every buy and sell, and two per ladder build.
    // 0 disables (fetch every time).
    planCacheMs: z.number().int().nonnegative().default(3_000),
    // Paper / twin / shadow swap-fee model (work plan 2026-09-25 P1.1, F4).
    //   tiered — PumpSwap canonical market-cap tiers per leg (on-chain
    //            FeeConfig when fetched, else the documented schedule in
    //            positions/feeTiers.ts). 1.25 %/leg at graduation mcap.
    //   flat   — legacy swapFeePct on both legs of the entry notional. Kept
    //            only as an emergency fallback; ~5x too cheap for graduations.
    feeModel: z.enum(['tiered', 'flat']).default('tiered'),
    // Refresh cadence of the on-chain PumpSwap FeeConfig (tier table).
    feeConfigRefreshMs: z.number().int().positive().default(600_000),
  })
  .strict();

/**
 * Honest simulator (work plan 2026-09-25 P1.2/P1.3, F5/F6): paper, dry-run
 * twin and shadow fills pay sampled confirm latency and fill pessimism
 * instead of filling at the trigger tick. Off by default so unit tests keep
 * deterministic instant fills; config.yaml turns it on.
 */
const SimulatorConfig = z
  .object({
    enabled: z.boolean().default(false),
    // PRNG seed for latency / haircut / failure draws. Same seed + same tick
    // stream => same fills.
    seed: z.number().int().default(1),
    // Use empirical latency_samples once a kind has at least this many rows;
    // below it, the lognormal defaults below.
    minSamples: z.number().int().positive().default(30),
    reloadMs: z.number().int().positive().default(3_600_000),
    // Lognormal defaults (median, p90), from recorded live data:
    // entry_confirm median 644 / p90 1097 ms (schema execution comment);
    // exit confirm avg 1257 ms (strategy-week SUMMARY) with p90 at 2.5x.
    entryConfirmMedianMs: positive.default(644),
    entryConfirmP90Ms: positive.default(1_097),
    exitConfirmMedianMs: positive.default(1_257),
    exitConfirmP90Ms: positive.default(3_143),
    // Extra adverse entry fill beyond constant-product impact, % of price,
    // triangular(min, mode, max).
    entryHaircutPct: z
      .object({ min: nonNeg.default(0), mode: nonNeg.default(0.5), max: nonNeg.default(3) })
      .strict()
      .default({}),
    // Residual random entry-failure rate on top of the mechanistic one
    // (price moved past the buy slippage bound during the confirm latency,
    // or the dry-run executor's own buy simulate rejected).
    baseEntryFailPct: pct.default(5),
    // Use the dry-run executor's real buy simulate as the landing test.
    useExecutorSimulation: z.boolean().default(true),
  })
  .strict();

/**
 * Transaction execution plumbing shared by the entry/exit executor and the H4
 * sellability probe.
 */
const ExecutionConfig = z
  .object({
    // Commitment for SDK pool-state reads and pre-send simulation. Detection
    // now fires before on-chain confirmation and the enricher reads pool
    // accounts at 'processed'; a 'confirmed' simulate could not see a pool
    // created 1–2 slots earlier ("Pool account not found" — 27% of H4
    // unknowns on 2026-09-17). Keep both legs at the same commitment.
    stateCommitment: z.enum(['processed', 'confirmed']).default('processed'),
    // Hard cap on a single simulateTransaction round-trip. web3.js has no
    // default fetch timeout, so a stalled RPC would otherwise hang an entry
    // indefinitely. Distinct from the CONFIRMATION timeout (12 s): a failed
    // simulate returns in < 1 s and is reported as "buy simulation failed".
    simulateTimeoutMs: z.number().int().positive().default(12_000),
    // Post-buy token-balance reconcile: attempts × delay. A single read at
    // 'confirmed' straight after confirmation raced the ledger on 2026-09-16
    // ("confirmed buy but wallet has no base tokens").
    reconcileAttempts: z.number().int().positive().default(4),
    reconcileDelayMs: z.number().int().nonnegative().default(400),
    /**
     * Cache the recent blockhash for this long. Blockhashes stay valid ~60-90 s,
     * so this is a large safety margin. It removes one serial round trip from
     * the pre-send path and — the bigger win — one PER TIER from
     * ExitLadder.refresh(), which paid 4 serial getLatestBlockhash calls every
     * 45 s per position and once while opening a live position.
     *
     * A stale blockhash is invisible to simulation (the simulate passes
     * replaceRecentBlockhash: true) and only bites at send, where the
     * broadcaster invalidates and retries once at no cost — the tx never landed.
     * 0 disables (fetch every assemble).
     */
    blockhashCacheMs: z.number().int().nonnegative().default(10_000),
    /**
     * Buy confirmation budget. The Broadcaster defaults (12 s / 500 ms) are for
     * exits that override them; buys inherited them unchanged. Set from
     * production data: latency_samples kind='entry_confirm' has median 644 ms,
     * p90 1097 ms, max 1135 ms — so 4 s is ~3.5x the observed worst case, while
     * 12 s pinned a concurrency slot and reserved SOL for a strategy whose
     * losers die in 3-7 s. This shortens time-to-give-up, not time-to-fill.
     */
    buyConfirmTimeoutMs: z.number().int().positive().default(4_000),
    buyConfirmPollMs: z.number().int().positive().default(250),
    /**
     * Simulate all buy slippage tiers CONCURRENTLY and send only the tightest
     * one that passes, instead of discovering a 6004 serially one tier at a
     * time (each serial round costing a fresh state read + assemble + simulate).
     *
     * Deliberately NOT "send before simulate": both tiers are independently
     * valid transactions and sends use skipPreflight, so both could land and buy
     * 2x size. Mutual exclusion would need a durable nonce account.
     */
    parallelBuySimulate: z.boolean().default(true),
  })
  .strict();

const RiskConfig = z
  .object({
    // Master switch for every entry-gating circuit breaker (Section 8:
    // DAILY_LOSS, CONSECUTIVE_LOSSES, EMERGENCY_EXITS, WALLET_FLOOR,
    // STREAM_DOWN, KILL_SWITCH, plus the H10 KILL-file sentinel). Testing
    // only — when true, canEnter() always passes and no breaker trips.
    // Default false (disabled for now): breakers stay enforced. Restart to
    // apply (hot-reload is NOT supported).
    disableAllBreakers: z.boolean().default(false),
    maxConcurrentPositions: z.number().int().positive().default(2),
    dailyLossLimitSol: z.coerce.number().positive().default(1.5),
    // Alternative daily cap as a fraction of wallet; the smaller of the two applies.
    dailyLossLimitWalletPct: z.coerce.number().min(0).max(100).default(5),
    consecutiveLossHalt: z.number().int().positive().default(4),
    consecutiveLossHaltMinutes: positive.default(120),
    dryRunConsecutiveLossHaltMinutes: positive.default(10),
    emergencyExitCount24h: z.number().int().positive().default(2),
    streamDownGraceMs: z.number().int().positive().default(10_000),
  })
  .strict();

const AlertsConfig = z
  .object({
    telegramBotTokenEnvVar: z.string().default('TG_BOT_TOKEN'),
    chatId: z.union([z.string(), z.number()]).optional(),
    // Telegram user IDs permitted to issue admin commands (/kill, blacklist edits).
    adminUserIds: z.array(z.number().int()).default([]),
    // Long-polling getUpdates loop for /kill + /status. Only ONE instance may
    // poll per bot token — a second poller gets 409 Conflict and steals
    // updates from the first. Set false on every instance but one.
    commandsEnabled: z.boolean().default(true),
  })
  .strict();

const PersistenceConfig = z
  .object({
    dbPath: z.string().default('./data/scalper.db'),
    priceTickRetentionDays: z.number().int().positive().default(7),
  })
  .strict();

const DashboardConfig = z
  .object({
    enabled: z.boolean().default(false),
    host: z.string().min(1).default('127.0.0.1'),
    port: z.number().int().positive().max(65_535).default(8787),
    usernameEnvVar: z.string().min(1).default('DASHBOARD_USERNAME'),
    passwordEnvVar: z.string().min(1).default('DASHBOARD_PASSWORD'),
  })
  .strict();

/**
 * Helius webhook ingest for pool-reserve updates. Pushes PriceTicks into the
 * shadow + dry-run twin trackers between their poll ticks (polling stays as
 * the liveness fallback; live exits stay poll-only). Requires a public URL
 * for Helius delivery plus a shared secret — without both, the route stays
 * disabled and polling carries on unchanged.
 */
const WebhooksConfig = z
  .object({
    enabled: z.boolean().default(false),
    secretEnvVar: z.string().min(1).default('HELIUS_WEBHOOK_SECRET'),
  })
  .strict();

/**
 * Program IDs are pinned in core/constants.ts but overridable here — pump.fun /
 * PumpSwap interfaces change (Section 13). A startup assertion verifies they
 * exist on-chain when a live RPC is configured.
 */
const ProgramOverrides = z
  .object({
    pumpFun: z.string().optional(),
    pumpSwap: z.string().optional(),
    raydiumAmm: z.string().optional(),
  })
  .strict()
  .default({});

export const ConfigSchema = z
  .object({
    mode: RunMode.default('paper'),
    // Verify pinned program IDs exist on-chain at startup. Requires a reachable
    // RPC; disable for offline/CI boots.
    assertProgramIdsOnChain: z.boolean().default(true),
    wallet: WalletConfig.default({}),
    // Optional so a fresh paper boot needs no credentials. Detection (Phase 1)
    // and live mode require it — enforced below and at detector startup.
    rpc: RpcConfig.optional(),
    jito: JitoConfig.optional(),
    detector: DetectorConfig.default({}),
    entry: EntryConfig.default({}),
    guardrails: GuardrailsConfig.default({}),
    exits: ExitsConfig.default({}),
    positions: PositionsConfig.default({}),
    shadow: ShadowConfig.default({}),
    launchTrack: LaunchTrackConfig.default({}),
    pregrad: PregradConfig.default({}),
    dryRunTwin: DryRunTwinConfig.default({}),
    fees: FeesConfig.default({}),
    simulator: SimulatorConfig.default({}),
    execution: ExecutionConfig.default({}),
    risk: RiskConfig.default({}),
    alerts: AlertsConfig.default({}),
    persistence: PersistenceConfig.default({}),
    dashboard: DashboardConfig.default({}),
    webhooks: WebhooksConfig.default({}),
    programs: ProgramOverrides,
  })
  .strict()
  .superRefine((cfg, ctx) => {
    // Live mode has stricter requirements than paper/dry-run.
    if (cfg.mode === 'live') {
      if (!cfg.rpc) {
        ctx.addIssue({
          code: z.ZodIssueCode.custom,
          path: ['rpc'],
          message: 'rpc config is required in live mode',
        });
      }
    }
    // gRPC feeds need an endpoint.
    if (cfg.detector.laserstreamEnabled && !cfg.rpc?.primaryGrpc) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        path: ['rpc', 'primaryGrpc'],
        message: 'rpc.primaryGrpc is required when detector.laserstreamEnabled is true',
      });
    }
    // At least one detection feed must be enabled.
    if (
      !cfg.detector.pumpportalEnabled &&
      !cfg.detector.laserstreamEnabled &&
      !cfg.detector.heliusWsEnabled
    ) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        path: ['detector'],
        message: 'enable at least one detection feed (pumpportalEnabled, heliusWsEnabled, or laserstreamEnabled)',
      });
    }
  });

export type Config = z.infer<typeof ConfigSchema>;
