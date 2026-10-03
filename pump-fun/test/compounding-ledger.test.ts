import { afterEach, describe, expect, it, vi } from 'vitest';
import { RiskManager } from '../src/risk/manager.ts';
import { PositionManager } from '../src/positions/manager.ts';
import type { PricePoller, PriceTick, PoolRef } from '../src/positions/pricing.ts';
import { TypedBus } from '../src/core/bus.ts';
import { Repositories } from '../src/persistence/repositories.ts';
import { openDb } from '../src/persistence/db.ts';
import { ConfigSchema } from '../src/config/schema.ts';
import { LAMPORTS_PER_SOL } from '../src/core/constants.ts';
import type { Executor } from '../src/executor/index.ts';
import type { BroadcastResult } from '../src/executor/broadcaster.ts';
import type { FillActuals } from '../src/executor/fillActuals.ts';
import type { PoolPricingRef } from '../src/core/types.ts';

const SOL = (n: number) => BigInt(Math.round(n * LAMPORTS_PER_SOL));
const liveCfg = (extra: Record<string, unknown> = {}) =>
  ConfigSchema.parse({ mode: 'live', rpc: { primaryHttp: 'https://rpc.example' }, wallet: { balanceFloorSol: 0.033, resyncSec: 300 }, ...extra });

afterEach(() => {
  vi.useRealTimers();
});

describe('RiskManager — idle-only resync of the in-memory balance', () => {
  function riskHarness(chain: { lamports: bigint }) {
    const repos = new Repositories(openDb({ path: ':memory:', memory: true }));
    let t = Date.UTC(2026, 9, 4, 12, 0, 0);
    let reads = 0;
    const risk = new RiskManager({
      config: liveCfg(),
      bus: new TypedBus(),
      repos,
      now: () => t,
      getWalletBalanceLamports: async () => {
        reads++;
        return chain.lamports;
      },
    });
    return { risk, repos, reads: () => reads, advance: (ms: number) => (t += ms) };
  }

  it('skips the chain read while busy and resyncs (recording drift) once idle', async () => {
    vi.useFakeTimers();
    const chain = { lamports: SOL(0.1) };
    const h = riskHarness(chain);
    await h.risk.refreshWalletBalance();
    let busy = true;
    h.risk.setBusyProbe(() => busy);
    h.risk.start();
    h.risk.applyBalanceDeltaSol(-0.02); // a trade in memory
    chain.lamports = SOL(0.0795);
    h.advance(300_000);
    await vi.advanceTimersByTimeAsync(300_000);
    expect(h.reads()).toBe(1); // boot only — skipped while busy
    expect(Number(h.risk.cachedBalanceLamports()) / LAMPORTS_PER_SOL).toBeCloseTo(0.08, 9);
    busy = false;
    h.advance(300_000);
    await vi.advanceTimersByTimeAsync(300_000);
    expect(h.reads()).toBe(2);
    expect(Number(h.risk.cachedBalanceLamports()) / LAMPORTS_PER_SOL).toBeCloseTo(0.0795, 9);
    const db = (h.repos as unknown as { db: { prepare(s: string): { all(): unknown[] } } }).db;
    const rows = db.prepare("select detail from wallet_events where kind = 'balance' order by id").all() as Array<{ detail: string | null }>;
    expect(JSON.parse(rows.at(-1)!.detail!)).toEqual({ driftLamports: -500_000 });
    h.risk.stop();
  });

  it('on-chain reconciles keep the ledger fresh, so WALLET_FLOOR does not fail closed between resyncs', async () => {
    const h = riskHarness({ lamports: SOL(0.1) });
    await h.risk.refreshWalletBalance();
    h.advance(2 * 300_000);
    h.risk.markLedgerFresh();
    h.advance(2 * 300_000); // 20 min since the chain read, 10 since the reconcile
    expect(h.risk.canEnter().ok).toBe(true);
    h.advance(3 * 300_000 + 1);
    expect(h.risk.canEnter()).toMatchObject({ ok: false, reason: 'WALLET_FLOOR' });
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
  tick(mint: string, price: number, atMs: number) {
    this.handler({ mint, price, baseReserve: 0n, quoteReserveLamports: 100n, atMs });
  }
}

const pricing = (): PoolPricingRef => ({
  poolAddress: 'pool', baseMint: 'mint', baseVault: 'bv', quoteVault: 'qv', baseDecimals: 6,
  baseReserve: 10n ** 15n, quoteReserveLamports: 100n * 10n ** 9n,
});
const ok = (signature: string): BroadcastResult => ({
  mode: 'live', simulated: true, sent: true, confirmed: true, signature, attempts: [{ route: 'p', submittedAtMs: 1, sent: true, signature }],
});
const fill = (signature: string, lamports: number): FillActuals => ({
  signature, slot: 1, walletLamportsDelta: lamports, feeLamports: 5_000, rentLamports: 0, tokenRawDelta: 0n, err: null,
});
const settle = async () => {
  for (let i = 0; i < 8; i++) await new Promise((r) => setTimeout(r, 0));
  await new Promise((r) => setTimeout(r, 20));
};

function managerHarness(executor: Partial<Executor>, cfg = liveCfg({ execution: { sentBuyResolveMs: 3, sentBuyPollMs: 1 } })) {
  const bus = new TypedBus();
  const repos = new Repositories(openDb({ path: ':memory:', memory: true }));
  const poller = new FakePoller();
  let balance = 0.1;
  const risk = {
    canEnter: () => ({ ok: true }),
    reserveSol: (s: number) => { balance -= s; },
    releaseSol: (s: number) => { balance += s; },
    applyBalanceDeltaSol: (d: number) => { balance += d; },
    markLedgerFresh: vi.fn(),
  };
  const mgr = new PositionManager({ config: cfg, bus, repos, poller: poller as unknown as PricePoller, executor: executor as Executor, risk });
  mgr.start();
  return { bus, mgr, poller, risk, balance: () => balance };
}

describe('PositionManager — compounding ledger reconciles each tx to its on-chain delta', () => {
  it('entry: the reserved size is corrected to the real spend (fees, tip, rent)', async () => {
    const executor: Partial<Executor> = {
      buyAndConfirm: vi.fn(async () => ok('buy')),
      reconcileTokenBalance: vi.fn(async () => 1_000_000_000n),
      buildExitLadder: vi.fn(() => ({ refresh: vi.fn(async () => undefined) }) as never),
      fillActuals: vi.fn(async () => fill('buy', -22_139_280)), // 0.02 + 0.0001 fees + 0.00203928 rent
    };
    const h = managerHarness(executor);
    h.bus.emit('openPosition', { mint: 'M', sizeSol: 0.02, highVolatility: false, pricing: pricing() });
    await settle();
    expect(h.balance()).toBeCloseTo(0.1 - 0.02213928, 9);
    expect(h.risk.markLedgerFresh).toHaveBeenCalled();
    expect(h.mgr.isBusy()).toBe(false);
    h.mgr.stop();
  });

  it('exit: the modelled credit lands at once, then is corrected to the sell tx delta', async () => {
    const cfg = liveCfg({ exits: { ladderSlippageTiers: [5], emergencySlippagePct: 90, maxExitAttempts: 2, exitRetryMs: 1 } });
    const fills: Record<string, FillActuals> = { buy: fill('buy', -20_000_000), sell: fill('sell', 13_000_000) };
    const executor: Partial<Executor> = {
      buyAndConfirm: vi.fn(async () => ok('buy')),
      reconcileTokenBalance: vi.fn().mockResolvedValueOnce(2_500_000_000_000n).mockResolvedValue(0n),
      buildExitLadder: vi.fn(() => ({ refresh: vi.fn(async () => undefined), isStale: vi.fn(() => true) }) as never),
      sellAndConfirm: vi.fn(async () => ok('sell')),
      fillActuals: vi.fn(async (sig: string) => fills[sig] ?? null),
    };
    const h = managerHarness(executor, cfg);
    h.bus.emit('openPosition', { mint: 'M', sizeSol: 0.02, highVolatility: false, pricing: pricing() });
    await settle();
    expect(h.balance()).toBeCloseTo(0.08, 9);
    h.poller.tick('M', 2e-9, 1000); // -75 %: hard stop -> full exit
    await settle();
    // Whatever the modelled credit was, the ledger ends on the chain's numbers.
    expect(h.balance()).toBeCloseTo(0.1 - 0.02 + 0.013, 9);
    expect(h.mgr.isBusy()).toBe(false);
    h.mgr.stop();
  });

  it('a sent buy that failed on-chain books only its fee', async () => {
    const executor: Partial<Executor> = {
      buyAndConfirm: vi.fn(async (): Promise<BroadcastResult> => ({ ...ok('bad'), confirmed: false, landingUnknown: false, sendErr: { Custom: 6004 } })),
      fillActuals: vi.fn(async () => fill('bad', -105_000)),
    };
    const h = managerHarness(executor);
    h.bus.emit('openPosition', { mint: 'M', sizeSol: 0.02, highVolatility: false, pricing: pricing() });
    await settle();
    expect(h.balance()).toBeCloseTo(0.1 - 0.000105, 9);
    h.mgr.stop();
  });

  it('is busy while an entry is resolving', async () => {
    let resolveBuy!: (r: BroadcastResult) => void;
    const executor: Partial<Executor> = {
      buyAndConfirm: vi.fn(() => new Promise<BroadcastResult>((r) => { resolveBuy = r; })),
      reconcileTokenBalance: vi.fn(async () => 1n),
      buildExitLadder: vi.fn(() => ({ refresh: vi.fn(async () => undefined) }) as never),
      fillActuals: vi.fn(async () => fill('buy', -20_000_000)),
    };
    const h = managerHarness(executor);
    h.bus.emit('openPosition', { mint: 'M', sizeSol: 0.02, highVolatility: false, pricing: pricing() });
    await new Promise((r) => setTimeout(r, 0));
    expect(h.mgr.isBusy()).toBe(true);
    resolveBuy(ok('buy'));
    await settle();
    expect(h.mgr.isBusy()).toBe(false);
    h.mgr.stop();
  });
});
