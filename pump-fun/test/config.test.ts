import { describe, it, expect } from 'vitest';
import { ConfigSchema } from '../src/config/schema.ts';

describe('config schema', () => {
  it('defaults to paper mode with sane defaults', () => {
    const cfg = ConfigSchema.parse({});
    expect(cfg.mode).toBe('paper');
    expect(cfg.entry.baseSizeWalletPct).toBe(8);
    expect(cfg.entry.minSizeWalletPct).toBe(5);
    expect(cfg.entry.maxSizeWalletPct).toBe(10);
    expect(cfg.entry.minAbsoluteSol).toBe(0.01);
    expect(cfg.exits.timeStopMinutes).toBe(15);
    expect(cfg.risk.maxConcurrentPositions).toBe(2);
    expect(cfg.guardrails.momentumWindowBucketsMs).toEqual([0, 250, 500, 750, 1000]);
    expect(cfg.guardrails.tolerateTxTooLargeSellability).toBe(false);
    expect(cfg.guardrails.tolerateInconclusiveSellability).toBe(false);
    expect(cfg.guardrails.relaxedRiskMaxReasons).toBe(1);
    expect(cfg.guardrails.relaxedRiskMaxSizeWalletPct).toBe(3);
  });

  it('enables the dry-run twin by default, inheriting the live poll cadence', () => {
    const cfg = ConfigSchema.parse({});
    expect(cfg.dryRunTwin.enabled).toBe(true);
    expect(cfg.dryRunTwin.maxConcurrent).toBe(8);
    expect(cfg.dryRunTwin.windowMinutes).toBe(20);
    expect(cfg.dryRunTwin.sizeMode).toBe('mirror');
    expect(cfg.dryRunTwin.coverBlocked).toBe(true);
    expect(cfg.dryRunTwin.coverFailed).toBe(true);
    // Quota-safe by default: a dedicated client spends RPC budget outside
    // rpc.maxConcurrentRequests and can starve live exit reads on a
    // rate-limited endpoint.
    expect(cfg.dryRunTwin.dedicatedRpc).toBe(false);
    // Omitted on purpose — index.ts falls back to positions.pricePollMs so the
    // twin can never poll slower than live and fake execution drag.
    expect(cfg.dryRunTwin.pollMs).toBeUndefined();
  });

  /**
   * config.yaml lists ${ENV_VAR} fallback slots for providers the operator may
   * not have signed up for yet. An unfilled slot interpolates to "" — that must
   * be dropped, not rejected, or adding a slot bricks the boot.
   */
  it('drops blank rpc fallback slots instead of failing validation', () => {
    const cfg = ConfigSchema.parse({
      rpc: {
        primaryHttp: 'https://primary',
        fallbackHttp: ['', '  ', 'https://fallback-a', 'https://fallback-b'],
      },
    });
    expect(cfg.rpc?.fallbackHttp).toEqual(['https://fallback-a', 'https://fallback-b']);
  });

  it('defaults rpc fallbackHttp to an empty list', () => {
    const cfg = ConfigSchema.parse({ rpc: { primaryHttp: 'https://primary' } });
    expect(cfg.rpc?.fallbackHttp).toEqual([]);
  });

  it('parses the Helius RPC fields (primary, gRPC, enrichment)', () => {
    const cfg = ConfigSchema.parse({
      rpc: {
        primaryHttp: 'https://mainnet.helius-rpc.com/?api-key=secret',
        primaryGrpc: 'https://laserstream-mainnet.helius-rpc.com',
        primaryGrpcTokenEnvVar: 'HELIUS_GRPC_TOKEN',
        enrichmentHttp: 'https://mainnet.helius-rpc.com/?api-key=secret',
      },
      detector: { laserstreamEnabled: true },
    });
    expect(cfg.rpc?.primaryHttp).toContain('helius-rpc.com');
    expect(cfg.rpc?.primaryGrpc).toContain('helius-rpc.com');
    expect(cfg.rpc?.primaryGrpcTokenEnvVar).toBe('HELIUS_GRPC_TOKEN');
  });

  it('drops a blank primaryGrpc slot instead of failing validation', () => {
    // config.yaml keeps `primaryGrpc: ${HELIUS_GRPC_URL}` wired at all times so
    // Business only needs .env + one flag flip. An unfilled slot interpolates
    // to "" — that must become undefined (feed self-skips), not a .min(1)
    // rejection that bricks the boot.
    const cfg = ConfigSchema.parse({
      rpc: { primaryHttp: 'https://mainnet.helius-rpc.com/?api-key=secret', primaryGrpc: '' },
    });
    expect(cfg.rpc?.primaryGrpc).toBeUndefined();
  });

  it('rejects unknown dryRunTwin keys (strict)', () => {
    expect(ConfigSchema.safeParse({ dryRunTwin: { bogus: true } }).success).toBe(false);
  });

  it('boots without an rpc block in paper mode', () => {
    const res = ConfigSchema.safeParse({ mode: 'paper' });
    expect(res.success).toBe(true);
  });

  it('requires rpc but not jito in live mode', () => {
    const res = ConfigSchema.safeParse({ mode: 'live' });
    expect(res.success).toBe(false);
    if (!res.success) {
      const paths = res.error.issues.map((i) => i.path.join('.'));
      expect(paths).toContain('rpc');
      expect(paths).not.toContain('jito');
    }
    expect(ConfigSchema.safeParse({ mode: 'live', rpc: { primaryHttp: 'http://x' } }).success).toBe(true);
  });

  it('rejects maxSizeWalletPct below baseSizeWalletPct', () => {
    const res = ConfigSchema.safeParse({ entry: { baseSizeWalletPct: 10, maxSizeWalletPct: 5 } });
    expect(res.success).toBe(false);
  });

  it('defaults entry percent rungs and dust floor', () => {
    const cfg = ConfigSchema.parse({});
    expect(cfg.entry.minSizeWalletPct).toBe(5);
    expect(cfg.entry.baseSizeWalletPct).toBe(8);
    expect(cfg.entry.maxSizeWalletPct).toBe(10);
    expect(cfg.entry.minAbsoluteSol).toBe(0.01);
  });

  it('coerces live risk percents from env-interpolated strings', () => {
    const cfg = ConfigSchema.parse({
      entry: { minSizeWalletPct: '5', baseSizeWalletPct: '5', maxSizeWalletPct: '5' },
      risk: { dailyLossLimitWalletPct: '5', dailyLossLimitSol: '0.02' },
    });
    expect(cfg.entry.baseSizeWalletPct).toBe(5);
    expect(cfg.risk.dailyLossLimitWalletPct).toBe(5);
    expect(cfg.risk.dailyLossLimitSol).toBe(0.02);
  });

  it('parses rpc.enrichmentHttp', () => {
    const cfg = ConfigSchema.parse({
      rpc: { primaryHttp: 'https://primary', enrichmentHttp: 'https://helius.test' },
    });
    expect(cfg.rpc?.enrichmentHttp).toBe('https://helius.test');
  });

  it('rejects minSizeWalletPct above maxSizeWalletPct', () => {
    const res = ConfigSchema.safeParse({ entry: { minSizeWalletPct: 20, maxSizeWalletPct: 10 } });
    expect(res.success).toBe(false);
  });

  it('rejects unknown top-level keys (strict)', () => {
    const res = ConfigSchema.safeParse({ bogus: true });
    expect(res.success).toBe(false);
  });

  it('rejects tp1SellFraction outside 0..1', () => {
    const res = ConfigSchema.safeParse({ exits: { tp1SellFraction: 1.5 } });
    expect(res.success).toBe(false);
  });
});

describe('detector liveness / migration authority / holders retry config', () => {
  it('applies defaults', () => {
    const cfg = ConfigSchema.parse({});
    expect(cfg.detector.migrationAuthority).toBe('39azUYFWPz3VHgKCf3VChUwbpURdCHRxjWVowf5jUJjg');
    expect(cfg.detector.liveness).toEqual({
      slotSilenceMs: 5_000,
      portalSilenceMs: 600_000,
      portalMissedGraduations: 3,
      onChainMissedGraduations: 3,
    });
    expect(cfg.detector.maxStaleSlots).toBe(150);
    expect(cfg.guardrails.holdersNotMintRetryDelaysMs).toEqual([0, 300, 600]);
  });

  it("accepts '' to disable authority narrowing and rejects unknown liveness keys", () => {
    expect(ConfigSchema.parse({ detector: { migrationAuthority: '' } }).detector.migrationAuthority).toBe('');
    expect(() => ConfigSchema.parse({ detector: { liveness: { bogus: 1 } } })).toThrow();
  });

  it('rejects a holders retry schedule that does not fit inside the enrichment budget', () => {
    expect(() =>
      ConfigSchema.parse({ guardrails: { enrichmentBudgetMs: 2500, holdersNotMintRetryDelaysMs: [0, 1000, 2000] } }),
    ).toThrow(/holdersNotMintRetryDelaysMs/);
    expect(
      ConfigSchema.parse({ guardrails: { enrichmentBudgetMs: 2500, holdersNotMintRetryDelaysMs: [0, 300, 700, 1200] } })
        .guardrails.holdersNotMintRetryDelaysMs,
    ).toEqual([0, 300, 700, 1200]);
  });
});
