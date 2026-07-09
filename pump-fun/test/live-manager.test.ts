import { describe, expect, it, vi } from 'vitest';
import { PositionManager } from '../src/positions/manager.ts';
import type { PricePoller, PriceTick, PoolRef } from '../src/positions/pricing.ts';
import { TypedBus } from '../src/core/bus.ts';
import { Repositories } from '../src/persistence/repositories.ts';
import { openDb } from '../src/persistence/db.ts';
import { ConfigSchema } from '../src/config/schema.ts';
import type { PoolPricingRef, Position } from '../src/core/types.ts';
import type { Executor } from '../src/executor/index.ts';
import type { BroadcastResult } from '../src/executor/broadcaster.ts';

class FakePoller {
  handler: (t: PriceTick) => void = () => {};
  registered = new Set<string>();
  setHandler(h: (t: PriceTick) => void) { this.handler = h; }
  register(r: PoolRef) { this.registered.add(r.mint); }
  unregister(m: string) { this.registered.delete(m); }
  start() {}
  stop() { this.registered.clear(); }
  get size() { return this.registered.size; }
  tick(mint: string, price: number, atMs: number) {
    this.handler({ mint, price, baseReserve: 0n, quoteReserveLamports: 100n, atMs });
  }
}

const confirmed = (signature: string): BroadcastResult => ({
  mode: 'live',
  simulated: true,
  sent: true,
  confirmed: true,
  signature,
  route: 'jito',
  attempts: [{ route: 'jito', submittedAtMs: 1, sent: true, signature }],
});

const cfg = ConfigSchema.parse({
  mode: 'live',
  rpc: { primaryHttp: 'https://rpc.example' },
  jito: { blockEngineUrl: 'https://mainnet.block-engine.jito.wtf' },
});

const pricing = (): PoolPricingRef => ({
  poolAddress: 'pool',
  baseMint: 'mint',
  baseVault: 'base-vault',
  quoteVault: 'quote-vault',
  baseDecimals: 6,
  baseReserve: 10n ** 15n,
  quoteReserveLamports: 100n * 10n ** 9n,
});

function harness(executor: Partial<Executor>) {
  const bus = new TypedBus();
  const repos = new Repositories(openDb({ path: ':memory:', memory: true }));
  const poller = new FakePoller();
  const mgr = new PositionManager({
    config: cfg,
    bus,
    repos,
    poller: poller as unknown as PricePoller,
    executor: executor as Executor,
  });
  mgr.start();
  return { bus, poller, mgr };
}

const flush = () => new Promise((resolve) => setTimeout(resolve, 0));

describe('PositionManager live execution', () => {
  it('does not mark OPEN until the buy confirms and token balance reconciles', async () => {
    let resolveBuy!: (r: BroadcastResult) => void;
    const buyPromise = new Promise<BroadcastResult>((resolve) => { resolveBuy = resolve; });
    const executor: Partial<Executor> = {
      buyAndConfirm: vi.fn(async () => buyPromise),
      reconcileTokenBalance: vi.fn(async () => 1_000_000n),
      buildExitLadder: vi.fn(() => ({ refresh: vi.fn(async () => undefined) }) as never),
    };
    const { bus, poller, mgr } = harness(executor);
    const updates: Position[] = [];
    bus.on('positionUpdate', (p) => updates.push(p));

    bus.emit('openPosition', { mint: 'M', sizeSol: 0.25, highVolatility: false, pricing: pricing() });
    await flush();
    expect(updates.map((u) => u.state)).toEqual(['PENDING_ENTRY']);
    expect(poller.size).toBe(0);

    resolveBuy(confirmed('entry-sig'));
    await flush();
    await flush();
    expect(updates.map((u) => u.state)).toEqual(['PENDING_ENTRY', 'OPEN']);
    expect(poller.size).toBe(1);
    mgr.stop();
  });

  it('uses actual raw token balance for TP1 sell sizing', async () => {
    const sellAndConfirm = vi.fn(async () => confirmed('exit-sig'));
    const executor: Partial<Executor> = {
      buyAndConfirm: vi.fn(async () => confirmed('entry-sig')),
      reconcileTokenBalance: vi.fn()
        .mockResolvedValueOnce(1_000_000n)
        .mockResolvedValueOnce(250_000n),
      buildExitLadder: vi.fn(() => ({ refresh: vi.fn(async () => undefined), isStale: vi.fn(() => false) }) as never),
      sellAndConfirm,
    };
    const { bus, poller, mgr } = harness(executor);

    bus.emit('openPosition', { mint: 'M', sizeSol: 0.25, highVolatility: false, pricing: pricing() });
    await flush();
    await flush();
    poller.tick('M', 1.5e-7, 1000);
    await flush();
    await flush();

    expect(sellAndConfirm).toHaveBeenCalledWith('pool', 'mint', 750_000n, 5);
    mgr.stop();
  });
});
