import { describe, it, expect } from 'vitest';
import {
  lognormalFromMedianP90,
  PendingExit,
  Simulator,
  triangular,
  type SimulatorCfg,
} from '../src/positions/simulator.ts';
import { FeeModel } from '../src/positions/feeModel.ts';
import { estimatePaperFeesTiered } from '../src/positions/paperFees.ts';
import { mulberry32, quantile } from '../src/research/stats.ts';
import { PositionManager } from '../src/positions/manager.ts';
import type { PricePoller, PriceTick, PoolRef } from '../src/positions/pricing.ts';
import { TypedBus } from '../src/core/bus.ts';
import { openDb } from '../src/persistence/db.ts';
import { Repositories } from '../src/persistence/repositories.ts';
import { ConfigSchema } from '../src/config/schema.ts';
import type { PoolPricingRef, Position } from '../src/core/types.ts';

const CFG: SimulatorCfg = {
  enabled: true,
  seed: 1,
  minSamples: 30,
  entryConfirmMedianMs: 644,
  entryConfirmP90Ms: 1097,
  exitConfirmMedianMs: 1257,
  exitConfirmP90Ms: 3143,
  entryHaircutPct: { min: 0, mode: 0.5, max: 3 },
  baseEntryFailPct: 0,
};

describe('simulator draws', () => {
  it('lognormal hits its median and p90', () => {
    const rng = mulberry32(5);
    const xs = Array.from({ length: 40_000 }, () => lognormalFromMedianP90(rng, 644, 1097));
    expect(quantile(xs, 0.5)).toBeGreaterThan(644 * 0.97);
    expect(quantile(xs, 0.5)).toBeLessThan(644 * 1.03);
    expect(quantile(xs, 0.9)).toBeGreaterThan(1097 * 0.95);
    expect(quantile(xs, 0.9)).toBeLessThan(1097 * 1.05);
  });

  it('triangular stays in range and centres near the mode', () => {
    const rng = mulberry32(9);
    const xs = Array.from({ length: 20_000 }, () => triangular(rng, 0, 0.5, 3));
    expect(Math.min(...xs)).toBeGreaterThanOrEqual(0);
    expect(Math.max(...xs)).toBeLessThanOrEqual(3);
    expect(xs.reduce((a, b) => a + b, 0) / xs.length).toBeCloseTo((0 + 0.5 + 3) / 3, 1);
  });

  it('is reproducible for a seed, and switches to empirical samples at minSamples', () => {
    const a = new Simulator(CFG);
    const b = new Simulator(CFG);
    const da = Array.from({ length: 5 }, () => a.sampleLatencyMs('exit_confirm'));
    const db = Array.from({ length: 5 }, () => b.sampleLatencyMs('exit_confirm'));
    expect(da).toEqual(db);
    expect(a.latencySource('exit_confirm')).toBe('lognormal');
    a.setSamples('exit_confirm', Array.from({ length: 30 }, () => 777));
    expect(a.latencySource('exit_confirm')).toBe('empirical');
    expect(a.sampleLatencyMs('exit_confirm')).toBe(777);
  });

  it('fails entries that moved past the slippage bound', () => {
    const sim = new Simulator(CFG);
    expect(sim.entryOutcome(9, 8)).toMatchObject({ ok: false, reason: 'slippage_exceeded' });
    expect(sim.entryOutcome(7.9, 8)).toEqual({ ok: true });
    const always = new Simulator({ ...CFG, baseEntryFailPct: 100 });
    expect(always.entryOutcome(0, 8)).toMatchObject({ ok: false, reason: 'random_failure' });
  });
});

describe('PendingExit', () => {
  it('stop fills at the worst price inside the confirm window', () => {
    const p = new PendingExit({ trigger: 'STOP_LOSS' as const, price: 85 }, 1_000, 1_200);
    expect(p.observe(80, 1_500)).toBe(false);
    expect(p.observe(83, 2_000)).toBe(false);
    expect(p.observe(60, 2_300)).toBe(true); // past due (2_200): not folded in
    expect(p.settlePrice()).toBe(80);
  });

  it('take-profit fills at the last price before the confirm lands', () => {
    const p = new PendingExit({ trigger: 'TAKE_PROFIT_1' as const, price: 115 }, 0, 1_000);
    p.observe(118, 400);
    p.observe(112, 900);
    expect(p.isDue(1_000)).toBe(true);
    expect(p.settlePrice()).toBe(112);
  });
});

describe('FeeModel', () => {
  it('flat mode charges swapFeePct; tiered uses the schedule', () => {
    expect(new FeeModel({ mode: 'flat', swapFeePct: 0.25 }).forMcap(380).bps).toBe(25);
    const tiered = new FeeModel({ mode: 'tiered', swapFeePct: 0.25 });
    expect(tiered.forPrice(3.8e-7)).toMatchObject({ bps: 125, source: 'static' });
    expect(tiered.forMcap(500).bps).toBe(120);
  });

  it('prefers the on-chain table and survives a failing loader', async () => {
    const m = new FeeModel({
      mode: 'tiered',
      swapFeePct: 0.25,
      loader: async () => [{ mcapSol: 0, creatorBps: 50, protocolBps: 50, lpBps: 0 }],
    });
    await m.refresh();
    expect(m.forMcap(380)).toMatchObject({ bps: 100, source: 'onchain' });
    const bad = new FeeModel({ mode: 'tiered', swapFeePct: 0.25, loader: async () => { throw new Error('rpc down'); } });
    await bad.refresh();
    expect(bad.forMcap(380)).toMatchObject({ bps: 125, source: 'static' });
  });

  it('a 0.03 SOL round trip at 380 SOL mcap costs 2 x 1.25 % + tx costs', () => {
    const fees = estimatePaperFeesTiered({
      entry: { valueSol: 0.03, feeBps: 125 },
      exits: [{ valueSol: 0.03, feeBps: 125 }],
      fees: { estPriorityTipSolPerTx: 0.0002 },
    });
    expect(fees).toBeCloseTo(2 * 0.0125 * 0.03 + 2 * 0.0002, 12);
  });
});

// ---------------------------------------------------------------------------
// PositionManager with the honest simulator on
// ---------------------------------------------------------------------------

class FakePoller {
  handler: (t: PriceTick) => void = () => {};
  readOnceResult: { price: number; baseReserve: bigint; quoteReserveLamports: bigint } | null = null;
  setHandler(h: (t: PriceTick) => void) { this.handler = h; }
  register(_r: PoolRef) {}
  unregister(_m: string) {}
  async readOnce() { return this.readOnceResult; }
  start() {}
  stop() {}
  get size() { return 0; }
  get pollStats() { return { failures: 0, deadlineExpired: 0, overlapSkips: 0 }; }
  tick(mint: string, price: number, atMs: number) {
    this.handler({ mint, price, baseReserve: 0n, quoteReserveLamports: 0n, atMs });
  }
}

const pricing = (): PoolPricingRef => ({
  poolAddress: 'pool', baseMint: 'mint', baseVault: 'b', quoteVault: 'q', baseDecimals: 6,
  baseReserve: 10n ** 15n, quoteReserveLamports: 100n * 10n ** 9n, // mid 1e-7
});

function simHarness(simOverride: Partial<SimulatorCfg> = {}) {
  const bus = new TypedBus();
  const db = openDb({ path: ':memory:', memory: true });
  const repos = new Repositories(db);
  const config = ConfigSchema.parse({ mode: 'paper' });
  const poller = new FakePoller();
  const clock = { t: 0 };
  const simulator = new Simulator({ ...CFG, entryHaircutPct: { min: 0, mode: 0, max: 0 }, ...simOverride });
  // Fixed latencies make the window assertions exact.
  simulator.setSamples('entry_confirm', Array.from({ length: 30 }, () => 600));
  simulator.setSamples('exit_confirm', Array.from({ length: 30 }, () => 1_000));
  const mgr = new PositionManager({
    config, bus, repos, poller: poller as unknown as PricePoller, now: () => clock.t,
    simulator,
    sleep: async (ms) => { clock.t += ms; },
  });
  mgr.start();
  return { bus, db, poller, clock, mgr };
}

const flush = () => new Promise((r) => setTimeout(r, 0));

describe('PositionManager + honest simulator', () => {
  it('enters after the confirm latency and stops out at the worst price in the exit window', async () => {
    const h = simHarness();
    const closed: Position[] = [];
    h.bus.on('positionUpdate', (p) => { if (p.state === 'CLOSED') closed.push(p); });
    h.poller.readOnceResult = { price: 1.02e-7, baseReserve: 10n ** 15n, quoteReserveLamports: 102n * 10n ** 9n };

    h.bus.emit('openPosition', { mint: 'S', sizeSol: 0.05, highVolatility: false, pricing: pricing(), detectedAtMs: 0 });
    await flush();
    expect(h.mgr.openCount).toBe(1);
    const open = h.db.prepare(`SELECT entry_price, detect_to_open_ms, simulated, entry_move_from_detect_pct FROM positions WHERE state='OPEN'`).get() as Record<string, number>;
    expect(open.entry_price).toBeCloseTo(1.02e-7, 15); // price at landing, not at screening
    expect(open.detect_to_open_ms).toBe(600);
    expect(open.simulated).toBe(1);
    expect(open.entry_move_from_detect_pct).toBeCloseTo(2, 6);

    h.clock.t = 1_000;
    h.poller.tick('S', 0.80e-7, 1_000); // -21.6 % -> STOP_LOSS (default 20 %) triggers
    expect(closed).toHaveLength(0); // not filled yet
    h.poller.tick('S', 0.70e-7, 1_500); // gap-through inside the window
    h.poller.tick('S', 0.80e-7, 1_900);
    h.clock.t = 2_100;
    h.poller.tick('S', 0.95e-7, 2_100); // past due: confirm already landed
    expect(closed).toHaveLength(1);
    const row = h.db.prepare(`SELECT exit_price, exit_trigger_to_confirm_ms, simulated, fee_tier_bps FROM positions WHERE state='CLOSED'`).get() as Record<string, number>;
    expect(row.exit_price).toBeCloseTo(0.70e-7, 15);
    expect(row.exit_trigger_to_confirm_ms).toBe(1_000);
    expect(row.simulated).toBe(1);
    expect(row.fee_tier_bps).toBe(125);
    // Simulated confirms never feed the latency pool.
    expect(h.db.prepare(`SELECT COUNT(*) AS n FROM latency_samples`).get()).toEqual({ n: 0 });
    h.mgr.stop();
  });

  it('records a FAILED simulated entry when the mid ran past the buy slippage bound', async () => {
    const h = simHarness();
    h.poller.readOnceResult = { price: 1.2e-7, baseReserve: 10n ** 15n, quoteReserveLamports: 120n * 10n ** 9n }; // +20 %
    h.bus.emit('openPosition', { mint: 'F', sizeSol: 0.05, highVolatility: false, pricing: pricing() });
    await flush();
    expect(h.mgr.openCount).toBe(0);
    const row = h.db.prepare(`SELECT state, simulated, execution_json FROM positions`).get() as Record<string, string | number>;
    expect(row.state).toBe('FAILED');
    expect(row.simulated).toBe(1);
    expect(JSON.parse(row.execution_json as string)).toMatchObject({ event: 'sim_entry_failed', reason: 'slippage_exceeded' });
    h.mgr.stop();
  });
});
