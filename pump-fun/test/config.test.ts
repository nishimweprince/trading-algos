import { describe, it, expect } from 'vitest';
import { ConfigSchema } from '../src/config/schema.ts';

describe('config schema', () => {
  it('defaults to paper mode with sane defaults', () => {
    const cfg = ConfigSchema.parse({});
    expect(cfg.mode).toBe('paper');
    expect(cfg.entry.baseSizeSol).toBe(0.25);
    expect(cfg.exits.timeStopMinutes).toBe(15);
    expect(cfg.risk.maxConcurrentPositions).toBe(2);
    expect(cfg.guardrails.momentumWindowBucketsMs).toEqual([0, 250, 500, 750, 1000]);
    expect(cfg.guardrails.tolerateTxTooLargeSellability).toBe(false);
    expect(cfg.guardrails.tolerateInconclusiveSellability).toBe(false);
    expect(cfg.guardrails.relaxedRiskMaxReasons).toBe(1);
    expect(cfg.guardrails.relaxedRiskMaxSizeSol).toBe(0.02);
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

  it('rejects maxSize below baseSize', () => {
    const res = ConfigSchema.safeParse({ entry: { baseSizeSol: 0.5, maxSizeSol: 0.25 } });
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
