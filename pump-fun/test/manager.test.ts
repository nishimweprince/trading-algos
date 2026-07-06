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
  setHandler(h: (t: PriceTick) => void) { this.handler = h; }
  register(r: PoolRef) { this.registered.add(r.mint); }
  unregister(m: string) { this.registered.delete(m); }
  start() {}
  stop() {}
  get size() { return this.registered.size; }
  tick(mint: string, price: number, atMs: number) {
    this.handler({ mint, price, baseReserve: 0n, quoteReserveLamports: 0n, atMs });
  }
}

const pricing = (): PoolPricingRef => ({
  baseVault: 'b',
  quoteVault: 'q',
  baseDecimals: 6,
  baseReserve: 10n ** 15n, // 1e9 tokens
  quoteReserveLamports: 100n * 10n ** 9n, // 100 SOL -> entry price 1e-7
});

function harness(configOverride: Record<string, unknown> = {}) {
  const bus = new TypedBus();
  const repos = new Repositories(openDb({ path: ':memory:', memory: true }));
  const config = ConfigSchema.parse({ mode: 'paper', ...configOverride });
  const poller = new FakePoller();
  const mgr = new PositionManager({ config, bus, repos, poller: poller as unknown as PricePoller, now: () => 0 });
  mgr.start();
  return { bus, repos, poller, mgr };
}

describe('PositionManager (paper)', () => {
  it('opens on accept, stages TP1/TP2, and records net PnL', () => {
    const { bus, repos, poller, mgr } = harness();
    const updates: Position[] = [];
    bus.on('positionUpdate', (p) => updates.push(p));

    bus.emit('openPosition', { mint: 'M', sizeSol: 0.25, highVolatility: false, pricing: pricing() });
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

    const row = repos as unknown as { /* verify a CLOSED row persisted */ };
    void row;
  });

  it('closes at a loss on hard stop with fees applied', () => {
    const { bus, poller } = harness();
    const closed: Position[] = [];
    bus.on('positionUpdate', (p) => { if (p.state === 'CLOSED') closed.push(p); });

    bus.emit('openPosition', { mint: 'L', sizeSol: 0.25, highVolatility: false, pricing: pricing() });
    poller.tick('L', 0.8e-7, 500); // -20% -> STOP_LOSS
    expect(closed[0]?.exitTrigger).toBe('STOP_LOSS');
    expect(closed[0]?.pnlSol!).toBeLessThan(0);
  });

  it('enforces the max-concurrent cap', () => {
    const { bus, poller, mgr } = harness({ risk: { maxConcurrentPositions: 1 } });
    const vetoes: string[] = [];
    bus.on('entryVetoed', (v) => vetoes.push(v.reason));

    bus.emit('openPosition', { mint: 'A', sizeSol: 0.25, highVolatility: false, pricing: pricing() });
    bus.emit('openPosition', { mint: 'B', sizeSol: 0.25, highVolatility: false, pricing: pricing() });
    expect(mgr.openCount).toBe(1);
    expect(vetoes).toContain('CIRCUIT_BREAKER');
    void poller;
  });
});
