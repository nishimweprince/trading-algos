import { describe, expect, it, vi } from 'vitest';
import { base58Encode, base58Decode } from '../src/core/base58.ts';
import { PROGRAM_IDS, WSOL_MINT } from '../src/core/constants.ts';
import { decodePool, decodeVirtualQuote, effectiveQuoteSol } from '../src/enrichment/pool.ts';
import { computePrice, revivePoolPricing, type PricePoller, type PriceTick, type PoolRef } from '../src/positions/pricing.ts';
import { PositionManager } from '../src/positions/manager.ts';
import { TypedBus } from '../src/core/bus.ts';
import { Repositories } from '../src/persistence/repositories.ts';
import { openDb } from '../src/persistence/db.ts';
import { ConfigSchema } from '../src/config/schema.ts';
import type { PoolPricingRef } from '../src/core/types.ts';
import type { Executor } from '../src/executor/index.ts';
import type { BroadcastResult } from '../src/executor/broadcaster.ts';

/**
 * 2026-10-04 incident: PumpSwap pools price against vault + virtual quote
 * reserve. Live graduation pools open at 67.41 SOL in the vault plus
 * 17.58 SOL virtual over 206.9M tokens; pricing the vault alone read 21.7 %
 * low and stopped every position out on its first tick.
 */
const VAULT = 67_405_855_261n;
const VIRTUAL = 17_584_505_324n;
const BASE = 206_900_000_000_000n; // 206.9M tokens, 6 decimals

const pk = (seed: number): string => base58Encode(Buffer.alloc(32).fill(seed));

function poolBuffer(virtualQuote: bigint | null): Buffer {
  const buf = Buffer.alloc(virtualQuote === null ? 243 : 270);
  Buffer.from('f19a6d0411b16dbc', 'hex').copy(buf, 0);
  const put = (addr: string, off: number) => Buffer.from(base58Decode(addr)).copy(buf, off);
  put(pk(6), 11);
  put(pk(1), 43);
  put(WSOL_MINT, 75);
  put(pk(3), 107);
  put(pk(4), 139);
  put(pk(5), 171);
  put(pk(7), 211);
  if (virtualQuote !== null) {
    buf.writeBigUInt64LE(virtualQuote & ((1n << 64n) - 1n), 245);
    buf.writeBigInt64LE(virtualQuote >> 64n, 253);
  }
  return buf;
}

describe('virtual quote reserves', () => {
  it('decodes virtual_quote_reserves (i128 @245) from the pool account', () => {
    const d = decodePool(poolBuffer(VIRTUAL).toString('base64'), PROGRAM_IDS.PUMP_SWAP, 'POOL');
    expect(d?.virtualQuoteLamports).toBe(VIRTUAL);
  });

  it('treats a legacy 243-byte pool as having no virtual reserve', () => {
    const d = decodePool(poolBuffer(null).toString('base64'), PROGRAM_IDS.PUMP_SWAP, 'POOL');
    expect(d?.virtualQuoteLamports).toBe(0n);
  });

  it('clamps a negative i128 to 0n', () => {
    expect(decodeVirtualQuote(poolBuffer(-5n))).toBe(0n);
  });

  it('prices a fresh graduation pool at vault + virtual', () => {
    const price = computePrice(BASE, VAULT, 6, VIRTUAL);
    expect(price).toBeCloseTo(4.108e-7, 9);
    const vaultOnly = computePrice(BASE, VAULT, 6, 0n);
    expect((vaultOnly / price - 1) * 100).toBeCloseTo(-20.7, 0);
  });

  it('agrees with a real live fill (FJpT…, 2026-10-04) within fee + impact', () => {
    // Pool state just before our buy (rounded from chain): 67.35 SOL vault, 207.03M tokens.
    const mark = computePrice(207_030_000_000_000n, 67_350_000_000n, 6, VIRTUAL);
    const fill = 0.0066760884 / (15_998_010_162 / 1e6); // SOL in / whole tokens out
    expect(fill / mark).toBeGreaterThan(1.0);
    expect(fill / mark).toBeLessThan(1.03); // 1.25 % fee + impact + rounding
    const vaultOnlyMark = computePrice(207_030_000_000_000n, 67_350_000_000n, 6, 0n);
    expect(fill / vaultOnlyMark).toBeGreaterThan(1.25); // the bug: a 27 % phantom gap
  });

  it('uses the effective quote for impact math', () => {
    expect(effectiveQuoteSol({ quoteReserveLamports: VAULT, virtualQuoteLamports: VIRTUAL })).toBeCloseTo(84.99, 2);
  });

  it('revives persisted pricing bigints and flags rows written before the field existed', () => {
    const fresh = revivePoolPricing(JSON.stringify({ poolAddress: 'p', baseMint: 'm', baseReserve: '10', quoteReserveLamports: '20', virtualQuoteLamports: '30' }));
    expect(fresh.virtualQuoteKnown).toBe(true);
    expect(fresh.pricing.baseReserve + fresh.pricing.quoteReserveLamports + fresh.pricing.virtualQuoteLamports).toBe(60n);
    const legacy = revivePoolPricing(JSON.stringify({ poolAddress: 'p', baseMint: 'm', baseReserve: '10', quoteReserveLamports: '20' }));
    expect(legacy.virtualQuoteKnown).toBe(false);
    expect(legacy.pricing.virtualQuoteLamports).toBe(0n);
  });
});

class FakePoller {
  handler: (t: PriceTick) => void = () => {};
  registered = new Set<string>();
  setHandler(h: (t: PriceTick) => void) { this.handler = h; }
  register(r: PoolRef) { this.registered.add(r.mint); }
  unregister(m: string) { this.registered.delete(m); }
  async readOnce() { return null; }
  start() {}
  stop() { this.registered.clear(); }
  get size() { return this.registered.size; }
  get pollStats() { return { cycles: 0, overlapSkips: 0, deadlineExpired: 0, failures: 0, lastErr: null }; }
}

const confirmed = (signature: string): BroadcastResult => ({
  mode: 'live', simulated: false, sent: true, confirmed: true, signature, route: 'primary',
  attempts: [{ route: 'primary', submittedAtMs: 1, sent: true, signature }],
});

describe('fill-vs-mark invariant', () => {
  const pricing = (virtualQuoteLamports: bigint): PoolPricingRef => ({
    poolAddress: 'pool', baseMint: 'mint', baseVault: 'bv', quoteVault: 'qv', baseDecimals: 6,
    baseReserve: BASE, quoteReserveLamports: VAULT, virtualQuoteLamports,
  });
  // 0.0066760884 SOL bought 16,148 tokens at the effective price + 1.25 % fee.
  const sizeSol = 0.0066760884;
  const rawAtRealPrice = BigInt(Math.round((sizeSol / (4.108e-7 * 1.0125)) * 1e6));

  function harness() {
    const bus = new TypedBus();
    const repos = new Repositories(openDb({ path: ':memory:', memory: true }));
    const executor: Partial<Executor> = {
      buyAndConfirm: vi.fn(async () => confirmed('entry-sig')),
      reconcileTokenBalance: vi.fn(async () => rawAtRealPrice),
      buildExitLadder: vi.fn(() => ({ refresh: vi.fn(async () => undefined) }) as never),
    };
    const mgr = new PositionManager({
      config: ConfigSchema.parse({ mode: 'live', rpc: { primaryHttp: 'https://rpc.example' } }),
      bus, repos, poller: new FakePoller() as unknown as PricePoller, executor: executor as Executor,
    });
    mgr.start();
    const alerts: string[] = [];
    bus.on('alert', (a) => alerts.push(a.message));
    return { bus, mgr, alerts };
  }
  const flush = () => new Promise((resolve) => setTimeout(resolve, 0));

  it('keeps trading when the mark includes the virtual reserve', async () => {
    const { bus, mgr } = harness();
    bus.emit('openPosition', { mint: 'M', sizeSol, highVolatility: false, pricing: pricing(VIRTUAL) });
    await flush();
    await flush();
    expect(mgr.entriesHalted).toBeNull();
    mgr.stop();
  });

  it('halts new entries when the fill is far from a vault-only mark', async () => {
    const { bus, mgr, alerts } = harness();
    bus.emit('openPosition', { mint: 'M', sizeSol, highVolatility: false, pricing: pricing(0n) });
    await flush();
    await flush();
    expect(mgr.entriesHalted).toMatch(/pricing is wrong/);
    expect(alerts.some((a) => a.includes('new entries halted'))).toBe(true);
    mgr.stop();
  });
});

describe('pricing_version column', () => {
  it('survives reopening the same database file (generated column migration is idempotent)', async () => {
    const { mkdtempSync } = await import('node:fs');
    const { tmpdir } = await import('node:os');
    const { join } = await import('node:path');
    const path = join(mkdtempSync(join(tmpdir(), 'pv-')), 'x.db');
    openDb({ path }).close();
    const db = openDb({ path });
    db.prepare(`INSERT INTO positions (mint, state, pricing_json) VALUES ('m', 'CLOSED', '{"virtualQuoteLamports":"1"}')`).run();
    expect((db.prepare(`SELECT pricing_version AS v FROM positions`).get() as { v: number }).v).toBe(2);
    db.close();
  });
});
