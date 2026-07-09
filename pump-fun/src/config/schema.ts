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
    // Net SOL inflow over the window at/above which the full momentum bonus is
    // awarded (linear, and symmetric for net outflow → penalty).
    momentumStrongInflowSol: positive.default(10),
    // Max soft-score points the momentum signal may add (or subtract).
    momentumMaxScoreBonus: nonNeg.default(15),
    // Inflow rate (SOL/sec) at/above which the candidate is flagged high-volatility,
    // tightening the trailing stop (exits.trailingGapHighVolPct).
    highVolInflowRateSolPerSec: positive.default(2),
  })
  .strict();

const ExitsConfig = z
  .object({
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
  })
  .strict();

const PositionsConfig = z
  .object({
    // Local price poll cadence per open position (free tier; gRPC gives per-slot).
    pricePollMs: z.number().int().positive().default(1000),
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
      if (!cfg.jito) {
        ctx.addIssue({
          code: z.ZodIssueCode.custom,
          path: ['jito'],
          message: 'jito config is required in live mode',
        });
      }
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
