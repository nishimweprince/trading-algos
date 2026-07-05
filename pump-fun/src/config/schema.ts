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
    primaryHttp: z.string().min(1),
    primaryGrpc: z.string().min(1),
    // gRPC auth token env var name (Yellowstone x-token). Optional for some providers.
    primaryGrpcTokenEnvVar: z.string().optional(),
    secondaryHttp: z.string().min(1),
    pumpportalWs: z.string().url().default('wss://pumpportal.fun/api/data'),
  })
  .strict();

const JitoConfig = z
  .object({
    blockEngineUrl: z.string().min(1),
    tipCapLamports: z.number().int().positive().default(2_000_000),
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
    timeStopMinutes: positive.default(15),
    emergencyLpDropPct: pct.default(15),
    // Ladder refresh cadence — blockhashes expire in ~60-90s (Section 7.2).
    ladderRefreshMs: z.number().int().positive().default(45_000),
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
    entry: EntryConfig.default({}),
    guardrails: GuardrailsConfig.default({}),
    exits: ExitsConfig.default({}),
    risk: RiskConfig.default({}),
    alerts: AlertsConfig.default({}),
    persistence: PersistenceConfig.default({}),
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
  });

export type Config = z.infer<typeof ConfigSchema>;
