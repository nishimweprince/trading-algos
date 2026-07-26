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
  readOnceCalls = 0;
  setHandler(h: (t: PriceTick) => void) { this.handler = h; }
  register(r: PoolRef) { this.registered.add(r.mint); }
  unregister(m: string) { this.registered.delete(m); }
  async readOnce() {
    this.readOnceCalls++;
    return null;
  }
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
});

const RAW_AT_STALE_PRICE = 2_500_000_000_000n;
const RAW_AFTER_TP1 = 625_000_000_000n;

const pricing = (): PoolPricingRef => ({
  poolAddress: 'pool',
  baseMint: 'mint',
  baseVault: 'base-vault',
  quoteVault: 'quote-vault',
  baseDecimals: 6,
  baseReserve: 10n ** 15n,
  quoteReserveLamports: 100n * 10n ** 9n,
});

function harness(executor: Partial<Executor>, config = cfg) {
  const bus = new TypedBus();
  const repos = new Repositories(openDb({ path: ':memory:', memory: true }));
  const poller = new FakePoller();
  const mgr = new PositionManager({
    config,
    bus,
    repos,
    poller: poller as unknown as PricePoller,
    executor: executor as Executor,
  });
  mgr.start();
  return { bus, repos, poller, mgr };
}

const flush = () => new Promise((resolve) => setTimeout(resolve, 0));

describe('PositionManager live execution', () => {
  it('does not mark OPEN until the buy confirms and token balance reconciles', async () => {
    let resolveBuy!: (r: BroadcastResult) => void;
    const buyPromise = new Promise<BroadcastResult>((resolve) => { resolveBuy = resolve; });
    const executor: Partial<Executor> = {
      buyAndConfirm: vi.fn(async () => buyPromise),
      reconcileTokenBalance: vi.fn(async () => RAW_AT_STALE_PRICE),
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

  it('uses the reconciled live fill price and does not fresh-read reserves', async () => {
    const executor: Partial<Executor> = {
      buyAndConfirm: vi.fn(async () => confirmed('entry-sig')),
      reconcileTokenBalance: vi.fn(async () => RAW_AT_STALE_PRICE / 2n),
      buildExitLadder: vi.fn(() => ({ refresh: vi.fn(async () => undefined) }) as never),
    };
    const { bus, poller, mgr } = harness(executor);
    const updates: Position[] = [];
    bus.on('positionUpdate', (p) => updates.push(p));

    bus.emit('openPosition', { mint: 'M', sizeSol: 0.25, highVolatility: false, momentumWindowMs: 500, pricing: pricing() });
    await flush();
    await flush();

    expect(poller.readOnceCalls).toBe(0);
    expect(updates.find((u) => u.state === 'OPEN')?.entryPrice).toBe(2e-7);
    mgr.stop();
  });

  // failLiveEntry already emits positionUpdate FAILED; the catch-all exception
  // path did not, leaving entry exceptions invisible to any bus subscriber
  // tracking entry outcomes (including the dry-run twin's attribution).
  it('emits positionUpdate FAILED when the live entry throws', async () => {
    const executor: Partial<Executor> = {
      buyAndConfirm: vi.fn(async () => { throw new Error('rpc exploded'); }),
    };
    const { bus, repos, mgr } = harness(executor);
    const updates: Position[] = [];
    bus.on('positionUpdate', (p) => updates.push(p));

    bus.emit('openPosition', { mint: 'M', sizeSol: 0.25, highVolatility: false, pricing: pricing() });
    await flush();
    await flush();

    expect(updates.map((u) => u.state)).toEqual(['PENDING_ENTRY', 'FAILED']);
    // The persisted row and the bus event must agree.
    expect(repos.latestOpenPositions()).toHaveLength(0);
    mgr.stop();
  });

  it('uses actual raw token balance for TP1 sell sizing', async () => {
    const sellAndConfirm = vi.fn(async () => confirmed('exit-sig'));
    const executor: Partial<Executor> = {
      buyAndConfirm: vi.fn(async () => confirmed('entry-sig')),
      reconcileTokenBalance: vi.fn()
        .mockResolvedValueOnce(RAW_AT_STALE_PRICE)
        .mockResolvedValueOnce(RAW_AFTER_TP1),
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

    expect(sellAndConfirm).toHaveBeenCalledWith('pool', 'mint', 1_875_000_000_000n, 5);
    mgr.stop();
  });

  it('persists EXITING before the first live sell broadcast resolves', async () => {
    let resolveSell!: (r: BroadcastResult) => void;
    const sellPromise = new Promise<BroadcastResult>((resolve) => { resolveSell = resolve; });
    const executor: Partial<Executor> = {
      buyAndConfirm: vi.fn(async () => confirmed('entry-sig')),
      reconcileTokenBalance: vi.fn()
        .mockResolvedValueOnce(RAW_AT_STALE_PRICE)
        .mockResolvedValueOnce(RAW_AFTER_TP1),
      buildExitLadder: vi.fn(() => ({ refresh: vi.fn(async () => undefined), isStale: vi.fn(() => false) }) as never),
      sellAndConfirm: vi.fn(async () => sellPromise),
    };
    const { bus, repos, poller, mgr } = harness(executor);

    bus.emit('openPosition', { mint: 'M', sizeSol: 0.25, highVolatility: false, pricing: pricing() });
    await flush();
    await flush();
    poller.tick('M', 1.5e-7, 1000);
    await flush();

    const exiting = repos.latestExitingPositions();
    expect(exiting).toHaveLength(1);
    expect(exiting[0]?.exitIntentJson).toContain('"status":"pending"');
    expect(executor.sellAndConfirm).toHaveBeenCalledWith('pool', 'mint', 1_875_000_000_000n, 5);

    resolveSell(confirmed('exit-sig'));
    await flush();
    await flush();
    mgr.stop();
  });

  it('keeps full live exits in EXITING when sell is unconfirmed and tokens remain', async () => {
    const fastExitCfg = ConfigSchema.parse({
      mode: 'live',
      rpc: { primaryHttp: 'https://rpc.example' },
      exits: { maxExitAttempts: 1, exitRetryMs: 1 },
    });
    const unconfirmed: BroadcastResult = {
      mode: 'live',
      simulated: true,
      sent: true,
      confirmed: false,
      signature: 'exit-sig',
      route: 'rpc',
      sendErr: 'confirmation timeout',
      attempts: [{ route: 'rpc', submittedAtMs: 1, sent: true, signature: 'exit-sig' }],
    };
    const executor: Partial<Executor> = {
      buyAndConfirm: vi.fn(async () => confirmed('entry-sig')),
      reconcileTokenBalance: vi.fn(async () => RAW_AT_STALE_PRICE),
      buildExitLadder: vi.fn(() => ({ refresh: vi.fn(async () => undefined), isStale: vi.fn(() => true) }) as never),
      sellAndConfirm: vi.fn(async () => unconfirmed),
    };
    const { bus, repos, poller, mgr } = harness(executor, fastExitCfg);
    const killSwitches: string[] = [];
    bus.on('killSwitch', (e) => killSwitches.push(e.detail ?? ''));

    bus.emit('openPosition', { mint: 'M', sizeSol: 0.25, highVolatility: false, pricing: pricing() });
    await flush();
    await flush();
    poller.tick('M', 0.7e-7, 1000);
    await flush();
    await flush();

    expect(repos.latestExitingPositions()).toHaveLength(1);
    expect(repos.latestOpenPositions()).toHaveLength(0);
    expect(killSwitches.some((d) => d.includes('live exit unresolved'))).toBe(true);
    mgr.stop();
  });

  it('recovers a live OPEN row and preserves momentum metadata into a recovered exit', async () => {
    let resolveSell!: (r: BroadcastResult) => void;
    const sellPromise = new Promise<BroadcastResult>((resolve) => { resolveSell = resolve; });
    const refresh = vi.fn(async () => undefined);
    const executor: Partial<Executor> = {
      reconcileTokenBalance: vi.fn(async () => RAW_AT_STALE_PRICE),
      buildExitLadder: vi.fn(() => ({ refresh, isStale: vi.fn(() => true) }) as never),
      sellAndConfirm: vi.fn(async () => sellPromise),
    };
    const { bus, repos, poller, mgr } = harness(executor);
    const killSwitches: string[] = [];
    bus.on('killSwitch', (e) => killSwitches.push(e.detail ?? ''));
    repos.upsertPosition({
      mint: 'M',
      state: 'OPEN',
      sizeSol: 0.25,
      entryPrice: 1e-7,
      openedAt: 100,
    }, {
      entryTx: 'entry-sig',
      rawBaseAmount: RAW_AT_STALE_PRICE / 2n,
      pricingJson: JSON.stringify(pricing(), (_k, v) => (typeof v === 'bigint' ? v.toString() : v)),
      executionJson: '{"event":"open"}',
      momentumWindowMs: 750,
    });

    await mgr.recoverOpenPositions();

    expect(killSwitches).toEqual([]);
    expect(mgr.openCount).toBe(1);
    expect(poller.size).toBe(1);
    expect(executor.reconcileTokenBalance).toHaveBeenCalledWith('mint', false);
    expect(refresh).toHaveBeenCalledWith(RAW_AT_STALE_PRICE);

    poller.tick('M', 0.7e-7, 1000);
    await flush();

    const exiting = repos.latestExitingPositions();
    expect(exiting).toHaveLength(1);
    expect(exiting[0]?.momentumWindowMs).toBe(750);
    expect(executor.sellAndConfirm).toHaveBeenCalled();

    resolveSell(confirmed('exit-sig'));
    await flush();
    await flush();
    mgr.stop();
  });

  it('engages the kill switch when EXITING recovery metadata is malformed', async () => {
    const executor: Partial<Executor> = {
      reconcileTokenBalance: vi.fn(async () => RAW_AT_STALE_PRICE),
      buildExitLadder: vi.fn(() => ({ refresh: vi.fn(async () => undefined), isStale: vi.fn(() => true) }) as never),
    };
    const { bus, repos, mgr } = harness(executor);
    const killSwitches: string[] = [];
    bus.on('killSwitch', (e) => killSwitches.push(e.detail ?? ''));
    repos.upsertPosition({
      mint: 'M',
      state: 'EXITING',
      sizeSol: 0.25,
      entryPrice: 1e-7,
      openedAt: 100,
      exitTrigger: 'STOP_LOSS',
    }, {
      rawBaseAmount: RAW_AT_STALE_PRICE,
      pricingJson: JSON.stringify(pricing(), (_k, v) => (typeof v === 'bigint' ? v.toString() : v)),
      exitIntentJson: '{bad-json',
    });

    await mgr.recoverExitingPositions();

    expect(killSwitches).toHaveLength(1);
    expect(killSwitches[0]).toContain('exit recovery failed');
    mgr.stop();
  });

  /**
   * The worst failure mode the dry-run twin could cause: crash recovery picking
   * up a simulated row and driving a REAL exit send path for a mint the wallet
   * never held. The executor stub throws on every method, so any contact fails
   * the test loudly.
   */
  it('crash recovery never resurrects a dry-run twin row as a live position', async () => {
    const executor: Partial<Executor> = {
      reconcileTokenBalance: vi.fn(async () => { throw new Error('recovery touched the executor'); }),
      buildExitLadder: vi.fn(() => { throw new Error('recovery built an exit ladder'); }),
      sellAndConfirm: vi.fn(async () => { throw new Error('recovery attempted a sell'); }),
    };
    const { bus, repos, mgr } = harness(executor);
    const killSwitches: string[] = [];
    bus.on('killSwitch', (e) => killSwitches.push(e.detail ?? ''));

    repos.upsertDryRunPosition({
      mint: 'M',
      state: 'OPEN',
      liveStatus: 'blocked_concurrent',
      sizeSol: 0.25,
      entryPrice: 1e-7,
      openedAt: 100,
    });
    repos.upsertDryRunPosition({
      mint: 'N',
      state: 'CLOSED',
      liveStatus: 'entry_failed',
      sizeSol: 0.25,
      entryPrice: 1e-7,
      exitReason: 'STOP_LOSS',
      openedAt: 100,
      closedAt: 200,
      netPnlSol: -0.2,
    });

    await mgr.recoverExitingPositions();
    await mgr.recoverOpenPositions();

    expect(executor.reconcileTokenBalance).not.toHaveBeenCalled();
    expect(executor.buildExitLadder).not.toHaveBeenCalled();
    expect(executor.sellAndConfirm).not.toHaveBeenCalled();
    expect(killSwitches).toHaveLength(0);
    expect(mgr.openCount).toBe(0);
    mgr.stop();
  });
});
