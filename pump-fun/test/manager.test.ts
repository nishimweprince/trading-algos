import { describe, it, expect } from 'vitest';
import { PositionManager } from '../src/positions/manager.ts';
import type { PricePoller, PriceTick, PoolRef } from '../src/positions/pricing.ts';
import { TypedBus } from '../src/core/bus.ts';
import { openDb } from '../src/persistence/db.ts';
import { Repositories } from '../src/persistence/repositories.ts';
import { ConfigSchema } from '../src/config/schema.ts';
import type { PoolPricingRef, Position } from '../src/core/types.ts';

/** Fake poller: captures the manager's handler so tests can push synthetic ticks. */
class FakePoller {
  handler: (t: PriceTick) => void = () => {};
  registered = new Set<string>();
  readOnceResult: Awaited<ReturnType<PricePoller['readOnce']>> = null;
  readOnceCalls = 0;
  setHandler(h: (t: PriceTick) => void) { this.handler = h; }
  register(r: PoolRef) { this.registered.add(r.mint); }
  unregister(m: string) { this.registered.delete(m); }
  async readOnce() {
    this.readOnceCalls++;
    return this.readOnceResult;
  }
  start() {}
  stop() {}
  get size() { return this.registered.size; }
  tick(mint: string, price: number, atMs: number, quoteReserveLamports = 0n, creatorBaseBalance?: bigint) {
    this.handler({
      mint,
      price,
      baseReserve: 0n,
      quoteReserveLamports,
      atMs,
      ...(creatorBaseBalance !== undefined ? { creatorBaseBalance } : {}),
    });
  }
}

const sol = (n: number) => BigInt(Math.floor(n * 1e9));

const pricing = (): PoolPricingRef => ({
  poolAddress: 'pool',
  baseMint: 'mint',
  baseVault: 'b',
  quoteVault: 'q',
  baseDecimals: 6,
  baseReserve: 10n ** 15n, // 1e9 tokens
  quoteReserveLamports: 100n * 10n ** 9n, // 100 SOL -> entry price 1e-7
});

function harness(configOverride: Record<string, unknown> = {}) {
  const bus = new TypedBus();
  const db = openDb({ path: ':memory:', memory: true });
  const repos = new Repositories(db);
  const config = ConfigSchema.parse({ mode: 'paper', ...configOverride });
  const poller = new FakePoller();
  const mgr = new PositionManager({ config, bus, repos, poller: poller as unknown as PricePoller, now: () => 0 });
  mgr.start();
  return { bus, db, repos, poller, mgr };
}

const flush = () => new Promise((resolve) => setTimeout(resolve, 0));

describe('PositionManager (paper)', () => {
  it('opens on accept, stages TP1/TP2, and records net PnL', async () => {
    const { bus, db, poller, mgr } = harness();
    const updates: Position[] = [];
    const alerts: Array<{ message: string; telegram?: boolean }> = [];
    bus.on('positionUpdate', (p) => updates.push(p));
    bus.on('alert', (a) => alerts.push(a));

    bus.emit('openPosition', { mint: 'M', sizeSol: 0.25, highVolatility: false, momentumWindowMs: 750, pricing: pricing() });
    await flush();
    expect(mgr.openCount).toBe(1);
    expect(poller.size).toBe(1);

    poller.tick('M', 1.5e-7, 1000); // +50% -> TP1 (sell 75%)
    expect(mgr.openCount).toBe(1);
    poller.tick('M', 2.0e-7, 2000); // +100% -> TP2 (remainder)
    expect(mgr.openCount).toBe(0);
    expect(poller.size).toBe(0);

    const closed = updates.find((u) => u.state === 'CLOSED');
    expect(closed).toBeDefined();
    expect(closed!.exitTrigger).toBe('TAKE_PROFIT_2');
    expect(closed!.pnlSol!).toBeGreaterThan(0); // net of fees, staged +50%/+100% is profitable
    expect(alerts).toHaveLength(4);
    expect(alerts.every((a) => a.telegram === true)).toBe(true);
    expect(alerts.map((a) => a.message)).toEqual([
      expect.stringContaining('opened'),
      expect.stringContaining('exit'),
      expect.stringContaining('exit'),
      expect.stringContaining('closed'),
    ]);

    const rows = db.prepare(
      `SELECT state, momentum_window_ms AS momentumWindowMs FROM positions WHERE mint = ? ORDER BY rowid`,
    ).all('M') as Array<{ state: string; momentumWindowMs: number | null }>;
    expect(rows.map((r) => [r.state, r.momentumWindowMs])).toEqual([
      ['OPEN', 750],
      ['CLOSED', 750],
    ]);
  });

  it('closes at a loss on hard stop with fees applied', async () => {
    const { bus, poller } = harness();
    const closed: Position[] = [];
    bus.on('positionUpdate', (p) => { if (p.state === 'CLOSED') closed.push(p); });

    bus.emit('openPosition', { mint: 'L', sizeSol: 0.25, highVolatility: false, pricing: pricing() });
    await flush();
    poller.tick('L', 0.8e-7, 500); // -20% -> STOP_LOSS
    expect(closed[0]?.exitTrigger).toBe('STOP_LOSS');
    expect(closed[0]?.pnlSol!).toBeLessThan(0);
  });

  it('enforces the max-concurrent cap', async () => {
    const { bus, poller, mgr } = harness({ risk: { maxConcurrentPositions: 1 } });
    const vetoes: string[] = [];
    bus.on('entryVetoed', (v) => vetoes.push(v.reason));

    bus.emit('openPosition', { mint: 'A', sizeSol: 0.25, highVolatility: false, pricing: pricing() });
    bus.emit('openPosition', { mint: 'B', sizeSol: 0.25, highVolatility: false, pricing: pricing() });
    await flush();
    expect(mgr.openCount).toBe(1);
    expect(vetoes).toContain('CIRCUIT_BREAKER');
    void poller;
  });

  // `reason` alone cannot tell a concurrency cap from a risk breaker — both are
  // CIRCUIT_BREAKER. The dry-run twin attributes opportunity cost off `code`,
  // so the code must be machine-readable, not parsed out of `detail` prose.
  it('tags max-concurrent blocks with a machine-readable code', async () => {
    const { bus, mgr } = harness({ risk: { maxConcurrentPositions: 1 } });
    const codes: Array<string | undefined> = [];
    bus.on('entryVetoed', (v) => codes.push(v.code));

    bus.emit('openPosition', { mint: 'A', sizeSol: 0.25, highVolatility: false, pricing: pricing() });
    bus.emit('openPosition', { mint: 'B', sizeSol: 0.25, highVolatility: false, pricing: pricing() });
    await flush();
    expect(mgr.openCount).toBe(1);
    expect(codes).toContain('MAX_CONCURRENT');
  });

  it('tags risk-breaker and kill-switch blocks with distinct codes', async () => {
    const bus = new TypedBus();
    const db = openDb({ path: ':memory:', memory: true });
    const repos = new Repositories(db);
    const config = ConfigSchema.parse({ mode: 'paper' });
    const poller = new FakePoller();
    let decision: { ok: boolean; reason?: string; detail?: string } = {
      ok: false,
      reason: 'DAILY_LOSS_LIMIT',
      detail: 'over cap',
    };
    const mgr = new PositionManager({
      config,
      bus,
      repos,
      poller: poller as unknown as PricePoller,
      risk: { canEnter: () => decision },
      now: () => 0,
    });
    mgr.start();

    const events: Array<{ reason: string; code?: string }> = [];
    bus.on('entryVetoed', (v) => events.push({ reason: v.reason, ...(v.code ? { code: v.code } : {}) }));

    bus.emit('openPosition', { mint: 'A', sizeSol: 0.25, highVolatility: false, pricing: pricing() });
    await flush();
    expect(events).toEqual([{ reason: 'CIRCUIT_BREAKER', code: 'RISK_BREAKER' }]);

    decision = { ok: false, reason: 'KILL_SWITCH', detail: 'engaged' };
    bus.emit('openPosition', { mint: 'B', sizeSol: 0.25, highVolatility: false, pricing: pricing() });
    await flush();
    expect(events[1]).toEqual({ reason: 'KILL_SWITCH', code: 'KILL_SWITCH' });
    expect(mgr.openCount).toBe(0);
  });

  it('fires EMERGENCY_EXIT on an LP pull and auto-blacklists the creator', async () => {
    const { bus, repos, poller, mgr } = harness({ risk: { maxConcurrentPositions: 5 } });
    const closed: Position[] = [];
    bus.on('positionUpdate', (p) => { if (p.state === 'CLOSED') closed.push(p); });

    // Valid base58 pubkeys so ATA derivation (real PublicKey) succeeds.
    const DEV = 'Dt4gYgAUts6puim6yj4m874V5TuGUGFyZVv4RNpFvUkY';
    const MINT = '64W4CqYgGzco1Rm5cgHepUWPvqPWWTJ6NRRWwLVGpump';
    const p = { ...pricing(), baseMint: MINT, creator: DEV, baseIsToken2022: true };
    bus.emit('openPosition', { mint: MINT, sizeSol: 0.25, highVolatility: false, pricing: p });
    await flush();
    // Establish a reserve high, then drain it hard.
    poller.tick(MINT, 1e-7, 100, sol(100));
    poller.tick(MINT, 1e-7, 200, sol(100));
    poller.tick(MINT, 0.9e-7, 300, sol(70)); // -30% SOL reserve -> LP_PULL

    expect(mgr.openCount).toBe(0);
    expect(closed[0]?.exitTrigger).toBe('EMERGENCY_EXIT');
    expect(repos.isMintBlacklisted(MINT)).toBe(true);
    expect(repos.isCreatorBlacklisted(DEV)).toBe(true);
  });

  it('force-closes all open positions on killSwitch', async () => {
    const { bus, mgr } = harness({ risk: { maxConcurrentPositions: 5 } });
    const closed: Position[] = [];
    bus.on('positionUpdate', (p) => { if (p.state === 'CLOSED') closed.push(p); });

    bus.emit('openPosition', { mint: 'A', sizeSol: 0.25, highVolatility: false, pricing: pricing() });
    bus.emit('openPosition', { mint: 'B', sizeSol: 0.25, highVolatility: false, pricing: pricing() });
    await flush();
    expect(mgr.openCount).toBe(2);

    bus.emit('killSwitch', { source: 'telegram' });
    expect(mgr.openCount).toBe(0);
    expect(closed.map((c) => c.exitTrigger)).toEqual(['KILL_SWITCH', 'KILL_SWITCH']);
  });

  it('uses a fresh read as the paper entry basis when available', async () => {
    const { bus, poller } = harness();
    const updates: Position[] = [];
    bus.on('positionUpdate', (p) => updates.push(p));
    poller.readOnceResult = {
      price: 2e-7,
      baseReserve: 10n ** 15n,
      quoteReserveLamports: 200n * 10n ** 9n,
    };

    bus.emit('openPosition', { mint: 'FRESH', sizeSol: 0.25, highVolatility: false, pricing: pricing() });
    await flush();

    expect(poller.readOnceCalls).toBe(1);
    expect(updates.find((u) => u.state === 'OPEN')?.entryPrice).toBe(2e-7);
  });

  it('falls back to enrichment reserves when the fresh read misses', async () => {
    const { bus, poller } = harness();
    const updates: Position[] = [];
    bus.on('positionUpdate', (p) => updates.push(p));
    poller.readOnceResult = null;

    bus.emit('openPosition', { mint: 'STALE_OK', sizeSol: 0.25, highVolatility: false, pricing: pricing() });
    await flush();

    expect(poller.readOnceCalls).toBe(1);
    expect(updates.find((u) => u.state === 'OPEN')?.entryPrice).toBe(1e-7);
  });
});
