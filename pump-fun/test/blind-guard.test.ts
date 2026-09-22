import { describe, expect, it, vi } from 'vitest';
import { PositionManager } from '../src/positions/manager.ts';
import type { PricePoller, PriceTick, PoolRef, PriceRead } from '../src/positions/pricing.ts';
import { TypedBus } from '../src/core/bus.ts';
import { Repositories } from '../src/persistence/repositories.ts';
import { openDb } from '../src/persistence/db.ts';
import { ConfigSchema } from '../src/config/schema.ts';
import type { PoolPricingRef, ExitTrigger } from '../src/core/types.ts';
import type { Executor } from '../src/executor/index.ts';
import type { BroadcastResult } from '../src/executor/broadcaster.ts';

/**
 * The blind-position guard.
 *
 * The exit FSM is driven purely by price ticks, so a position with no ticks is
 * unmanaged: it cannot stop out, trail or take profit. Forensics on 25 live
 * positions found 14 that exited on a SINGLE price observation — 0 wins, median
 * -22.6%, realized stop-loss mean -24.9% against a -15% configured stop, and
 * TAKE_PROFIT_1 never reached once in live history. `exits.timeStopMinutes`
 * (600_000 ms) was the only no-tick net, ~100x too slow for a failure mode that
 * kills positions in 3-7 s.
 *
 * Unlike the other manager suites these tests use a MUTABLE clock, because the
 * guard is entirely time-driven.
 */

class FakePoller {
  handler: (t: PriceTick) => void = () => {};
  registered = new Set<string>();
  readOnceCalls = 0;
  readOnceResult: PriceRead | null = null;
  setHandler(h: (t: PriceTick) => void) { this.handler = h; }
  register(r: PoolRef) { this.registered.add(r.mint); }
  unregister(m: string) { this.registered.delete(m); }
  async readOnce(): Promise<PriceRead | null> {
    this.readOnceCalls++;
    return this.readOnceResult;
  }
  start() {}
  stop() { this.registered.clear(); }
  get size() { return this.registered.size; }
  get pollStats() {
    return { cycles: 0, overlapSkips: 0, deadlineExpired: 0, failures: 0, lastErr: null };
  }
  tick(mint: string, price: number, atMs: number, quoteReserveLamports = 100n) {
    this.handler({ mint, price, baseReserve: 1_000_000n, quoteReserveLamports, atMs });
  }
}

const confirmed = (signature: string): BroadcastResult => ({
  mode: 'live',
  simulated: true,
  sent: true,
  confirmed: true,
  signature,
  route: 'primary',
  attempts: [{ route: 'primary', submittedAtMs: 1, sent: true, signature }],
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

const RAW = 2_500_000_000_000n;

/** An empty ladder: isStale() true forces the fresh-sell fallback path. */
const emptyLadder = () =>
  ({ refresh: vi.fn(async () => undefined), isStale: vi.fn(() => true), size: 0 }) as never;

function harness(overrides: Partial<Executor> = {}, configOverride: Record<string, unknown> = {}) {
  const clock = { v: 0 };
  const config = ConfigSchema.parse({
    mode: 'live',
    rpc: { primaryHttp: 'https://rpc.example' },
    ...configOverride,
  });
  const bus = new TypedBus();
  const repos = new Repositories(openDb({ path: ':memory:', memory: true }));
  const poller = new FakePoller();
  let sold = false;
  const sellAndConfirm = vi.fn(async () => {
    sold = true;
    return confirmed('exit-sig');
  });
  const executor: Partial<Executor> = {
    buyAndConfirm: vi.fn(async () => confirmed('entry-sig')),
    // Holds the bought balance until a sell lands, then reads empty — which is
    // what lets a full exit settle from EXITING through to CLOSED.
    reconcileTokenBalance: vi.fn(async () => (sold ? 0n : RAW)),
    buildExitLadder: vi.fn(emptyLadder),
    sellAndConfirm,
    ...overrides,
  };
  const mgr = new PositionManager({
    config,
    bus,
    repos,
    poller: poller as unknown as PricePoller,
    executor: executor as Executor,
    now: () => clock.v,
  });
  mgr.start();

  const alerts: Array<{ level: string; message: string }> = [];
  bus.on('alert', (a) => alerts.push(a));
  const exits: Array<{ mint: string; trigger: ExitTrigger; detail?: string }> = [];
  bus.on('exitTriggered', (e) => exits.push(e));
  const killSwitches: Array<{ source: string; detail?: string }> = [];
  bus.on('killSwitch', (k) => killSwitches.push(k));

  return { bus, repos, poller, mgr, clock, alerts, exits, killSwitches, sellAndConfirm, executor };
}

const flush = () => new Promise((resolve) => setTimeout(resolve, 0));

/** Drive the watchdog deterministically rather than waiting on its interval. */
const watchdog = (mgr: PositionManager) => (mgr as unknown as { runWatchdog(): void }).runWatchdog();

async function openOne(h: ReturnType<typeof harness>, mint = 'M') {
  h.bus.emit('openPosition', { mint, sizeSol: 0.25, highVolatility: false, pricing: pricing() });
  await flush();
  await flush();
}

describe('blind-position guard', () => {
  it('forces a direct vault read when no tick arrives, without exiting', async () => {
    const h = await Promise.resolve(harness());
    await openOne(h);
    expect(h.poller.size).toBe(1);

    h.clock.v = 1_600; // past blindFirstTickMs (1500), short of blindExitMs (4000)
    watchdog(h.mgr);
    await flush();

    expect(h.poller.readOnceCalls).toBe(1);
    expect(h.exits).toHaveLength(0);
    expect(h.mgr.openCount).toBe(1);
    h.mgr.stop();
  });

  it('issues the force-read only once per position', async () => {
    const h = harness();
    await openOne(h);

    h.clock.v = 1_600;
    watchdog(h.mgr);
    await flush();
    h.clock.v = 2_200;
    watchdog(h.mgr);
    await flush();

    expect(h.poller.readOnceCalls).toBe(1);
    h.mgr.stop();
  });

  it('recovers when the forced read returns a usable price', async () => {
    const h = harness();
    // Close to the 1e-7 entry: a big move here would trip TP1 and mask the point.
    h.poller.readOnceResult = { price: 1.02e-7, baseReserve: 1_000_000n, quoteReserveLamports: 200n };
    await openOne(h);

    h.clock.v = 1_600;
    watchdog(h.mgr);
    await flush();

    expect(h.poller.readOnceCalls).toBe(1);
    expect(h.exits).toHaveLength(0);

    // The synthesised tick counts, so the position is no longer blind: the exit
    // threshold is now measured from the tick, not from entry.
    h.clock.v = 4_500;
    watchdog(h.mgr);
    await flush();
    expect(h.exits).toHaveLength(0);
    expect(h.mgr.openCount).toBe(1);
    h.mgr.stop();
  });

  it('closes at market as NO_PRICE_DATA when the forced read also fails', async () => {
    const h = harness();
    await openOne(h);

    h.clock.v = 4_100; // past blindExitMs
    watchdog(h.mgr);
    await flush();
    await flush();

    expect(h.exits).toHaveLength(1);
    expect(h.exits[0]!.trigger).toBe('NO_PRICE_DATA');
    // The empty-ladder fallback must reach a fresh sell.
    expect(h.sellAndConfirm).toHaveBeenCalled();
    h.mgr.stop();
  });

  it('does not announce a PnL figure it does not have', async () => {
    const h = harness();
    await openOne(h);

    h.clock.v = 4_100;
    watchdog(h.mgr);
    await flush();

    const alert = h.alerts.find((a) => a.message.includes('NO_PRICE_DATA'));
    expect(alert).toBeDefined();
    expect(alert!.level).toBe('error');
    // The old wall-clock path announced a fabricated ~0 PnL, because lastPrice
    // was still the entry price when no tick ever arrived.
    expect(alert!.message).not.toMatch(/pnl [+-]?\d/);
    expect(alert!.message).toContain('pnl unknown until fill');
    h.mgr.stop();
  });

  it('does nothing while ticks are arriving', async () => {
    const h = harness();
    await openOne(h);
    h.poller.tick('M', 1.01e-7, 500);
    await flush();

    h.clock.v = 1_600;
    watchdog(h.mgr);
    await flush();
    expect(h.poller.readOnceCalls).toBe(0);

    // Well past blindExitMs, but inside blindStaleTickMs measured from the tick.
    h.clock.v = 4_500;
    watchdog(h.mgr);
    await flush();
    expect(h.exits).toHaveLength(0);
    h.mgr.stop();
  });

  it('catches mid-life starvation and force-closes at the last usable price', async () => {
    const h = harness();
    await openOne(h);
    h.poller.tick('M', 1.2e-7, 500); // +20% over the 1e-7 entry
    await flush();

    h.clock.v = 500 + 6_100; // past blindStaleTickMs from the last tick
    watchdog(h.mgr);
    await flush();
    await flush();

    expect(h.exits[0]!.trigger).toBe('NO_PRICE_DATA');
    // Sized off the reconciled balance at the last KNOWN price, not the entry.
    expect(h.sellAndConfirm).toHaveBeenCalled();
    h.mgr.stop();
  });

  it('halts entries via kill switch when several positions go blind at once', async () => {
    const h = harness();
    await openOne(h, 'M1');
    await openOne(h, 'M2');
    expect(h.mgr.openCount).toBe(2);

    h.clock.v = 4_100;
    watchdog(h.mgr);
    await flush();

    expect(h.killSwitches).toHaveLength(1);
    expect(h.killSwitches[0]!.source).toBe('internal');
    expect(h.killSwitches[0]!.detail).toContain('blind');
    expect(h.alerts.some((a) => a.message.includes('price feed down'))).toBe(true);
    h.mgr.stop();
  });

  it('is disabled by positions.blindGuardEnabled=false', async () => {
    const h = harness({}, { positions: { blindGuardEnabled: false } });
    await openOne(h);

    h.clock.v = 60_000;
    watchdog(h.mgr);
    await flush();

    expect(h.poller.readOnceCalls).toBe(0);
    expect(h.exits).toHaveLength(0);
    h.mgr.stop();
  });

  it('runs off the manager interval, not just a direct call', async () => {
    vi.useFakeTimers();
    try {
      const h = harness();
      h.bus.emit('openPosition', { mint: 'M', sizeSol: 0.25, highVolatility: false, pricing: pricing() });
      await vi.advanceTimersByTimeAsync(1);
      await vi.advanceTimersByTimeAsync(1);

      h.clock.v = 1_600;
      await vi.advanceTimersByTimeAsync(1_100); // one watchdog tick (max(pricePollMs,1000))

      expect(h.poller.readOnceCalls).toBeGreaterThanOrEqual(1);
      h.mgr.stop();
    } finally {
      vi.useRealTimers();
    }
  });
});

describe('suspect-tick guard', () => {
  it('rejects a zero price instead of reading it as -100%', async () => {
    const h = harness();
    await openOne(h);

    // computePrice returns 0 on an empty base reserve; a torn 'processed' read
    // of a fresh pool can produce exactly this.
    h.poller.tick('M', 0, 500);
    await flush();
    await flush();

    expect(h.exits).toHaveLength(0);
    expect(h.sellAndConfirm).not.toHaveBeenCalled();
    h.mgr.stop();
  });

  it('does not clobber lastPrice with a suspect value', async () => {
    const h = harness();
    await openOne(h);
    h.poller.tick('M', 1.2e-7, 400);
    await flush();
    h.poller.tick('M', 0, 500);
    await flush();
    h.poller.tick('M', Number.NaN, 600);
    await flush();

    // Blind-exit force-close uses lastPrice; it must still be the 1.2e-7 tick.
    h.clock.v = 400 + 6_100;
    watchdog(h.mgr);
    await flush();
    await flush();

    expect(h.exits[0]!.trigger).toBe('NO_PRICE_DATA');
    h.mgr.stop();
  });

  it('still fires the LP-pull emergency on a drained quote vault', async () => {
    const h = harness();
    await openOne(h);
    h.poller.tick('M', 1e-7, 100, 100n * 10n ** 9n); // seed the reserve baseline
    await flush();

    // Quote vault drained: price is 0 (suspect) but this is a genuine LP pull,
    // which the monitor must still see.
    h.poller.tick('M', 0, 200, 1n);
    await flush();
    await flush();

    expect(h.exits.map((e) => e.trigger)).toContain('EMERGENCY_EXIT');
    h.mgr.stop();
  });
});

describe('pricing registration ordering', () => {
  it('registers pricing before the initial exit-ladder refresh resolves', async () => {
    // This is the Cause C regression: openLive used to `await ladder.refresh()`
    // (4 serial getLatestBlockhash calls) BEFORE registering the poller, so a
    // position that dies in 3-7 s spent 25-50% of its life unpriced.
    const neverResolves = new Promise<undefined>(() => {});
    const h = harness({
      buildExitLadder: vi.fn(
        () => ({ refresh: vi.fn(() => neverResolves), isStale: vi.fn(() => true), size: 0 }) as never,
      ),
    });

    await openOne(h);

    expect(h.poller.registered.has('M')).toBe(true);
    expect(h.mgr.openCount).toBe(1);
    h.mgr.stop();
  });
});

describe('tick accounting persistence', () => {
  it('writes ticks_observed, suspect_ticks and first_tick_ms on the closed row', async () => {
    const h = harness();
    await openOne(h);

    h.poller.tick('M', 1.02e-7, 300); // first usable tick, 300ms after entry
    await flush();
    h.poller.tick('M', 0, 400); // suspect
    await flush();
    h.poller.tick('M', 1.03e-7, 500);
    await flush();
    h.poller.tick('M', 1.5e-7, 600); // TP1
    await flush();
    await flush();
    await flush();

    const db = (h.repos as unknown as { db: { prepare(sql: string): { get(): unknown } } }).db;
    const row = db
      .prepare(
        `select ticks_observed t, suspect_ticks s, first_tick_ms f, entry_move_from_detect_pct e
         from positions where mint = 'M' and state = 'CLOSED'`,
      )
      .get() as { t: number; s: number; f: number; e: number | null };

    // 4 ticks delivered, 1 of them suspect — so the FSM saw 3.
    expect(row.t).toBe(3);
    expect(row.s).toBe(1);
    expect(row.f).toBe(300);
    expect(row.e).not.toBeNull();
    h.mgr.stop();
  });
});
