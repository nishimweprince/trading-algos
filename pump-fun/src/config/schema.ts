import { z } from 'zod';

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
  })
  .strict();

const RpcConfig = z
  .object({
    // The only always-required endpoint (free Helius tier). Used for on-chain
    // confirmation and enrichment.
    primaryHttp: z.string().min(1),
    // gRPC (Yellowstone/LaserStream) — paid tier. Optional; required only when
    // detector.grpcEnabled is true (enforced below).
    primaryGrpc: z.string().min(1).optional(),
    // gRPC auth token env var name (Yellowstone x-token). Optional for some providers.
    primaryGrpcTokenEnvVar: z.string().optional(),
    // Second independent provider for redundant broadcast (Phase 4 / live).
    secondaryHttp: z.string().min(1).optional(),
    pumpportalWs: z.string().url().default('wss://pumpportal.fun/api/data'),
    // Cap concurrent in-flight RPC requests to stay under free-tier rate limits.
    maxConcurrentRequests: z.number().int().positive().default(4),
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

const DetectorConfig = z
  .object({
    // PumpPortal WebSocket — free, purpose-built migration events. Default feed.
    pumpportalEnabled: z.boolean().default(true),
    // Yellowstone gRPC — lowest latency, paid tier. Opt-in drop-in upgrade.
    grpcEnabled: z.boolean().default(false),
    // Helius WebSocket (logsSubscribe on the pump.fun program) — direct on-chain
    // feed using the existing Helius key (wss derived from rpc.primaryHttp).
    heliusWsEnabled: z.boolean().default(false),
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
  })
  .strict();

const EntryConfig = z
  .object({
    baseSizeSol: positive.default(0.25),
    maxSizeSol: positive.default(0.35),
    maxSlippagePct: pct.default(5),
    minEntryScore: z.number().min(0).max(100).default(60),
  })
  .strict()
  .refine((e) => e.maxSizeSol >= e.baseSizeSol, {
    message: 'entry.maxSizeSol must be >= entry.baseSizeSol',
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
    // Strict baselines used to tag "relaxed" accepts when config thresholds are
    // widened. Defaults match the researched v1 guardrail thresholds.
    strictTop10HolderCapPct: pct.default(25),
    strictCreatorHoldingsCapPct: pct.default(5),
    strictMinPoolSol: nonNeg.default(25),
    relaxedRiskMaxReasons: z.number().int().positive().default(1),
    relaxedRiskSizeMultiplierCap: positive.default(0.5),
    relaxedRiskMaxSizeSol: positive.default(0.02),
    relaxedRiskMaxOpenPositions: z.number().int().positive().default(1),
    relaxedRiskTimeStopMinutes: positive.default(10),
    relaxedRiskTrailingGapPct: positive.default(10),
    relaxedRiskEmergencyLpDropPct: pct.default(10),
    relaxedRiskTp0Enabled: z.boolean().default(true),
    // Global enrichment budget; anything slower is marked "unknown" (Section 5 / 6.3).
    enrichmentBudgetMs: z.number().int().positive().default(1500),
    // RugCheck advisory soft signal (Section 6.2). Off by default; the API key
    // (higher rate limits) is read from this env var when present.
    rugcheckEnabled: z.boolean().default(false),
    rugcheckApiKeyEnvVar: z.string().default('RUGCHECK_API_KEY'),
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
  .strict();

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
    emergencyLpDropPct: pct.default(15),
    // Rolling window (in price-poll ticks) for the LP-pull high-water mark.
    lpDropWindowTicks: z.number().int().positive().default(5),
    // In-position dev-dump monitor: fire EMERGENCY_EXIT when the creator sells
    // at least this % of their observed base-token holdings.
    creatorDumpEnabled: z.boolean().default(true),
    creatorDumpThresholdPct: pct.default(50),
    // Ladder refresh cadence — blockhashes expire in ~60-90s (Section 7.2).
    ladderRefreshMs: z.number().int().positive().default(45_000),
    // Pre-signed exit ladder slippage tiers (%), worst-case last. Escalation
    // walks from tightest to loosest; emergency exits jump to the last tier.
    ladderSlippageTiers: z.array(pct).nonempty().default([2, 5, 10, 25]),
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
    // Simulated entry size for fee-adjusted PnL. Defaults to entry.baseSizeSol
    // when omitted at wiring time (see index.ts).
    sizeSol: positive.optional(),
  })
  .strict();

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
    // Rough priority fee + Jito tip per transaction, in SOL.
    estPriorityTipSolPerTx: nonNeg.default(0.001),
    // Priority-fee floor/cap in micro-lamports per compute unit. The floor was
    // raised from the old hardcoded 50k (~0.0000125 SOL at 250k CU) because a
    // low-fee exit tx waits many slots for inclusion while a graduated token is
    // crashing — the #1 cause of stops realizing far worse than configured.
    priorityFloorMicroLamports: z.number().int().nonnegative().default(250_000),
    priorityCapMicroLamports: z.number().int().positive().default(5_000_000),
  })
  .strict();

const RiskConfig = z
  .object({
    maxConcurrentPositions: z.number().int().positive().default(2),
    dailyLossLimitSol: positive.default(1.5),
    // Alternative daily cap as a fraction of wallet; the smaller of the two applies.
    dailyLossLimitWalletPct: pct.default(5),
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
    dryRunTwin: DryRunTwinConfig.default({}),
    fees: FeesConfig.default({}),
    risk: RiskConfig.default({}),
    alerts: AlertsConfig.default({}),
    persistence: PersistenceConfig.default({}),
    dashboard: DashboardConfig.default({}),
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
    // gRPC feed needs an endpoint.
    if (cfg.detector.grpcEnabled && !cfg.rpc?.primaryGrpc) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        path: ['rpc', 'primaryGrpc'],
        message: 'rpc.primaryGrpc is required when detector.grpcEnabled is true',
      });
    }
    // At least one detection feed must be enabled.
    if (!cfg.detector.pumpportalEnabled && !cfg.detector.grpcEnabled && !cfg.detector.heliusWsEnabled) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        path: ['detector'],
        message: 'enable at least one detection feed (pumpportalEnabled, heliusWsEnabled, or grpcEnabled)',
      });
    }
  });

export type Config = z.infer<typeof ConfigSchema>;
