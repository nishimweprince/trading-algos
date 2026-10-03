import { describe, expect, it, vi } from 'vitest';
import { PositionManager } from '../src/positions/manager.ts';
import type { PricePoller, PriceTick, PoolRef, PriceIngest, TickSink } from '../src/positions/pricing.ts';
import { TypedBus } from '../src/core/bus.ts';
import { Repositories } from '../src/persistence/repositories.ts';
import { openDb } from '../src/persistence/db.ts';
import { ConfigSchema } from '../src/config/schema.ts';
import type { BroadcastResult } from '../src/executor/broadcaster.ts';
import { EmergencyMonitor } from '../src/positions/monitors.ts';

class FakePoller {
  handler: (t: PriceTick) => void = () => {};
  setHandler(h: (t: PriceTick) => void) { this.handler = h; }
  register(_r: PoolRef) {}
  unregister() {}
  async readOnce() { return null; }
  start() {}
  stop() {}
}

class FakeIngest implements PriceIngest {
  sinks = new Map<string, TickSink>();
  watchers = new Map<string, (raw: bigint, slot: number | undefined) => void>();
  register(ref: PoolRef, sink: TickSink) { this.sinks.set(ref.mint, sink); }
  unregister(mint: string) { this.sinks.delete(mint); }
  watchAccount(a: string, cb: (raw: bigint, slot: number | undefined) => void) { this.watchers.set(a, cb); }
  unwatchAccount(a: string) { this.watchers.delete(a); }
}

const RAW = 2_500_000_000_000n;
const pricing = { poolAddress: 'pool', baseMint: 'So11111111111111111111111111111111111111112', baseVault: 'bv', quoteVault: 'qv', baseDecimals: 6, baseReserve: 10n ** 15n, quoteReserveLamports: 10n ** 11n };
const ok = (signature: string): BroadcastResult => ({ mode: 'live', simulated: false, sent: true, confirmed: true, signature, attempts: [] });
const settle = async () => {
  for (let i = 0; i < 8; i++) await new Promise((r) => setTimeout(r, 0));
  await new Promise((r) => setTimeout(r, 20));
};

function harness(executor: Record<string, unknown>) {
  const cfg = ConfigSchema.parse({ mode: 'live', rpc: { primaryHttp: 'https://rpc.example' }, exits: { presignLadder: false, hardStopPct: 15 } });
  const poller = new FakePoller();
  const ingest = new FakeIngest();
  const bus = new TypedBus();
  const repos = new Repositories(openDb({ path: ':memory:', memory: true }));
  let balance = 0.1;
  const risk = { canEnter: () => ({ ok: true }), reserveSol: (s: number) => { balance -= s; }, releaseSol: (s: number) => { balance += s; }, applyBalanceDeltaSol: (d: number) => { balance += d; }, markLedgerFresh: vi.fn() };
  const mgr = new PositionManager({ config: cfg, bus, repos, poller: poller as unknown as PricePoller, ingest, executor: { publicKey: 'So11111111111111111111111111111111111111112', ...executor } as never, risk });
  mgr.start();
  return { mgr, poller, ingest, bus, repos, balance: () => balance };
}

describe('price freshness: a stale poll never overrides a newer push', () => {
  it('a poll read issued before the latest push cannot trigger a stop at its older, lower price', async () => {
    const sellAndConfirm = vi.fn(async () => ok('sell'));
    const h = harness({
      buyAndConfirm: vi.fn(async () => ok('buy')),
      reconcileTokenBalance: vi.fn(async () => RAW),
      sellAndConfirm,
    });
    h.bus.emit('openPosition', { mint: 'M', sizeSol: 0.02, highVolatility: false, pricing });
    await settle();
    // entry price = 0.02 / 2.5e6 = 8e-9
    const t = Date.now();
    h.ingest.sinks.get('M')!({ mint: 'M', price: 8.4e-9, baseReserve: 10n ** 15n, quoteReserveLamports: 10n ** 11n, atMs: t, source: 'push' });
    // A poll read issued 200 ms BEFORE that push lands now with a -25 % price.
    h.poller.handler({ mint: 'M', price: 6e-9, baseReserve: 10n ** 15n, quoteReserveLamports: 9n * 10n ** 10n, atMs: t + 5, source: 'poll', readStartedAtMs: t - 200 });
    await settle();
    expect(sellAndConfirm).not.toHaveBeenCalled();
    // A FRESH poll (issued after the push) at that price does trigger.
    h.poller.handler({ mint: 'M', price: 6e-9, baseReserve: 10n ** 15n, quoteReserveLamports: 9n * 10n ** 10n, atMs: t + 600, source: 'poll', readStartedAtMs: Date.now() + 1 });
    await settle();
    expect(sellAndConfirm).toHaveBeenCalled();
    h.mgr.stop();
  });
});

describe('exit lands at processed via our token account push', () => {
  it('resolves the exit from the push, credits a live-reserve estimate immediately, and corrects from actuals', async () => {
    let fired = false;
    const broadcastSignedExit = vi.fn(async (_b: Uint8Array, _m: string, landed?: Promise<unknown>) => {
      // Simulate the chain: the sell lands, our ATA push reports 0, and the push wakes the confirm wait.
      setTimeout(() => {
        fired = true;
        for (const cb of h.ingest.watchers.values()) cb(0n, 50);
      }, 5);
      await landed;
      return { mode: 'live', simulated: false, sent: true, confirmed: true, confirmationStatus: 'processed', signature: 'sell', attempts: [], submittedAtMs: Date.now() } as BroadcastResult;
    });
    const fillActuals = vi.fn(async (sig: string) =>
      sig === 'sell'
        ? { signature: 'sell', slot: 50, walletLamportsDelta: 7_000_000, feeLamports: 5_000, rentLamports: 0, tokenRawDelta: -RAW, err: null }
        : { signature: sig, slot: 1, walletLamportsDelta: -20_000_000, feeLamports: 5_000, rentLamports: 0, tokenRawDelta: RAW, err: null });
    const h = harness({
      buyAndConfirm: vi.fn(async () => ok('buy')),
      reconcileTokenBalance: vi.fn(async () => RAW),
      primeExitState: vi.fn(async () => undefined),
      dropExitState: vi.fn(),
      buildExitTx: vi.fn(async () => new Uint8Array([1])),
      broadcastSignedExit,
      readTokenBalance: vi.fn(async () => { throw new Error('push should make this unnecessary'); }),
      fillActuals,
    });
    const updates: string[] = [];
    h.bus.on('positionUpdate', (p) => updates.push(p.state));
    h.bus.emit('openPosition', { mint: 'M', sizeSol: 0.02, highVolatility: false, pricing });
    await settle();
    expect(h.ingest.watchers.size).toBe(1); // our ATA is watched while the position is open
    h.ingest.sinks.get('M')!({ mint: 'M', price: 2e-9, baseReserve: 4n * 10n ** 15n, quoteReserveLamports: 8n * 10n ** 9n, atMs: Date.now(), source: 'push' });
    await settle();
    expect(fired).toBe(true);
    expect(updates.at(-1)).toBe('CLOSED');
    // Ledger ends on the chain's number after the async correction.
    expect(h.balance()).toBeCloseTo(0.1 - 0.02 + 0.007, 9);
    expect(h.ingest.watchers.size).toBe(0); // unwatched on close
    h.mgr.stop();
  });
});

describe('the exit decision runs before the price tick is persisted', () => {
  it('no price_ticks row exists synchronously after the triggering tick; it lands right after', async () => {
    const h = harness({
      buyAndConfirm: vi.fn(async () => ok('buy')),
      reconcileTokenBalance: vi.fn(async () => RAW),
      sellAndConfirm: vi.fn(async () => ok('sell')),
    });
    h.bus.emit('openPosition', { mint: 'M', sizeSol: 0.02, highVolatility: false, pricing });
    await settle();
    const db = (h.repos as unknown as { db: { prepare(s: string): { get(): unknown } } }).db;
    const count = () => (db.prepare('select count(*) n from price_ticks').get() as { n: number }).n;
    const before = count();
    h.poller.handler({ mint: 'M', price: 2e-9, baseReserve: 4n * 10n ** 15n, quoteReserveLamports: 8n * 10n ** 9n, atMs: Date.now() });
    expect(count()).toBe(before);
    await new Promise((r) => setImmediate(r));
    expect(count()).toBe(before + 1);
    h.mgr.stop();
  });
});

describe('EmergencyMonitor — time-based LP window', () => {
  it('drops samples older than windowMs regardless of tick count', () => {
    const m = new EmergencyMonitor({ lpDropPct: 15, windowTicks: 1_000, windowMs: 5_000, creatorDumpEnabled: false, creatorDumpPct: 50 });
    expect(m.onTick({ atMs: 0, quoteReserveLamports: 100n })).toBeNull();
    // 6 s later the 100 high-water has aged out: a slow 20 % bleed is not an LP pull.
    expect(m.onTick({ atMs: 6_000, quoteReserveLamports: 80n })).toBeNull();
    // Within the window, a 20 % drop is.
    expect(m.onTick({ atMs: 7_000, quoteReserveLamports: 64n })?.kind).toBe('LP_PULL');
  });
});
