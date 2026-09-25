import { describe, expect, it } from 'vitest';
import { openDb } from '../src/persistence/db.ts';
import { Repositories, type DryRunPositionRow } from '../src/persistence/repositories.ts';
import { ConfigSchema } from '../src/config/schema.ts';
import { TypedBus } from '../src/core/bus.ts';
import { DryRunTracker } from '../src/positions/dryRunTracker.ts';
import type { RpcClient } from '../src/core/rpc.ts';
import type { PoolPricingRef } from '../src/core/types.ts';
import { buildTradeBlotterCsv } from '../src/dashboard/queries.ts';
import { feeBpsForMcap } from '../src/positions/feeTiers.ts';

/**
 * Phase 1 of LIVE_PILOT_PLAN.md — the twin must defend and pay like live:
 *   T1 emergency monitors, T2 slippage, T3 attribution, plus the S1 experiment
 *   lane (exit overrides stamped on every row).
 */

const rpc = new Proxy(
  {},
  {
    get(_t, prop: string) {
      if (prop === 'getMultipleAccountsBase64') return async () => [];
      if (prop === 'then') return undefined;
      throw new Error(`twin touched rpc.${prop}`);
    },
  },
) as unknown as RpcClient;

const SOL = 10n ** 9n;
const BASE = 10n ** 15n; // 1e9 tokens at 6 decimals
const QUOTE = 100n * SOL; // 100 SOL → mid 1e-7
const ENTRY = 1e-7;
// A real-looking creator so the ATA derives; base58 32-byte keys.
const CREATOR = '11111111111111111111111111111111';
const MINT = 'So11111111111111111111111111111111111111112';

const pricing = (): PoolPricingRef => ({
  poolAddress: 'pool',
  baseMint: MINT,
  baseVault: 'base-vault',
  quoteVault: 'quote-vault',
  baseDecimals: 6,
  baseReserve: BASE,
  quoteReserveLamports: QUOTE,
  creator: CREATOR,
  baseIsToken2022: false,
});

const flush = () => new Promise((r) => setTimeout(r, 0));

function harness(overrides: Record<string, unknown> = {}) {
  const bus = new TypedBus();
  const db = openDb({ path: ':memory:', memory: true });
  const repos = new Repositories(db);
  const config = ConfigSchema.parse({
    mode: 'live',
    rpc: { primaryHttp: 'http://x' },
    exits: { emergencyLpDropPct: 35, lpDropWindowTicks: 20, creatorDumpThresholdPct: 50 },
    ...overrides,
  });
  let clock = 0;
  const tracker = new DryRunTracker({ config, bus, repos, rpc, now: () => clock });
  tracker.start();
  return {
    bus,
    db,
    tracker,
    advance: (ms: number) => (clock += ms),
    clock: () => clock,
    accept: (extra: Record<string, unknown> = {}) =>
      bus.emit('openPosition', { mint: MINT, sizeSol: 0.1, highVolatility: false, pricing: pricing(), ...extra }),
    row: () => repos.latestDryRunPositions().find((r) => r.mint === MINT) as DryRunPositionRow,
  };
}

describe('T1 — twin runs the same emergency monitors as live', () => {
  it('closes as EMERGENCY_EXIT when the SOL reserve is pulled', async () => {
    const h = harness();
    h.accept();
    await flush();
    // Two steady ticks, then a 40% reserve drain in one tick (price barely moves
    // because the base side left too — the LP-pull signature the price stop misses).
    h.advance(500);
    h.tracker.injectTick(MINT, ENTRY, h.clock(), { baseReserve: BASE, quoteReserveLamports: QUOTE });
    h.advance(500);
    h.tracker.injectTick(MINT, ENTRY * 1.01, h.clock(), { baseReserve: BASE, quoteReserveLamports: QUOTE });
    h.advance(500);
    h.tracker.injectTick(MINT, ENTRY * 0.99, h.clock(), { baseReserve: BASE, quoteReserveLamports: (QUOTE * 60n) / 100n });
    await flush();
    const row = h.row();
    expect(row.state).toBe('CLOSED');
    expect(row.exit_reason).toBe('EMERGENCY_EXIT');
    expect(h.tracker.size).toBe(0);
    h.tracker.stop();
  });

  it('closes as EMERGENCY_EXIT when the creator dumps past the threshold', async () => {
    const h = harness();
    h.accept();
    await flush();
    h.advance(500);
    h.tracker.injectTick(MINT, ENTRY, h.clock(), { baseReserve: BASE, quoteReserveLamports: QUOTE, creatorBaseBalance: 1_000_000n });
    h.advance(500);
    h.tracker.injectTick(MINT, ENTRY * 1.02, h.clock(), { baseReserve: BASE, quoteReserveLamports: QUOTE, creatorBaseBalance: 400_000n });
    await flush();
    expect(h.row().exit_reason).toBe('EMERGENCY_EXIT');
    h.tracker.stop();
  });

  it('does not fire on ordinary post-graduation sell flow', async () => {
    const h = harness();
    h.accept();
    await flush();
    h.advance(500);
    h.tracker.injectTick(MINT, ENTRY * 1.05, h.clock(), { baseReserve: BASE, quoteReserveLamports: (QUOTE * 90n) / 100n });
    h.advance(500);
    h.tracker.injectTick(MINT, ENTRY * 1.04, h.clock(), { baseReserve: BASE, quoteReserveLamports: (QUOTE * 75n) / 100n });
    await flush();
    expect(h.row().state).toBe('OPEN');
    h.tracker.stop();
  });

  it('treats a vanished vault (reserve 0) as an LP pull, exactly as live does', async () => {
    const h = harness();
    h.accept();
    await flush();
    h.advance(500);
    h.tracker.injectTick(MINT, ENTRY, h.clock(), { baseReserve: BASE, quoteReserveLamports: QUOTE });
    h.advance(500);
    h.tracker.injectTick(MINT, ENTRY, h.clock()); // reserves 0n after a real read = vault gone
    await flush();
    expect(h.row().exit_reason).toBe('EMERGENCY_EXIT');
    h.tracker.stop();
  });
});

describe('T2 — twin pays constant-product impact', () => {
  it('records buy impact at entry and sell impact on the closing fill, and nets it out', async () => {
    const h = harness();
    h.accept();
    await flush();
    h.advance(1000);
    // Hard stop at -15%: sell into a pool that still holds BASE tokens.
    h.tracker.injectTick(MINT, ENTRY * 0.8, h.clock(), { baseReserve: BASE, quoteReserveLamports: (QUOTE * 80n) / 100n });
    await flush();
    const row = h.row();
    expect(row.exit_reason).toBe('STOP_LOSS');
    const buy = 0.1 * (0.1 / 100.1);
    const tokens = 0.1 / ENTRY; // 1e6 tokens
    const sell = tokens * ENTRY * 0.8 * (tokens / (1e9 + tokens));
    expect(row.slippage_sol!).toBeCloseTo(buy + sell, 9);
    // Tiered PumpSwap fees (P1.1): each leg pays its mcap tier on its own notional.
    expect(row.fees_sol! - row.slippage_sol!).toBeCloseTo(
      2 * ConfigSchema.parse({}).fees.estPriorityTipSolPerTx +
        0.1 * (feeBpsForMcap(ENTRY * 1e9) / 10_000) +
        0.1 * 0.8 * (feeBpsForMcap(ENTRY * 0.8 * 1e9) / 10_000),
      9,
    );
    expect(row.net_pnl_sol!).toBeCloseTo(row.gross_pnl_sol! - row.fees_sol!, 12);
    h.tracker.stop();
  });

  it('charges nothing when the model is off', async () => {
    const h = harness({ fees: { modelPaperSlippage: false } });
    h.accept();
    await flush();
    h.advance(1000);
    h.tracker.injectTick(MINT, ENTRY * 0.8, h.clock(), { baseReserve: BASE, quoteReserveLamports: QUOTE });
    await flush();
    expect(h.row().slippage_sol).toBe(0);
    h.tracker.stop();
  });
});

describe('T3 — attribution columns are filled on twin rows', () => {
  it('carries feed source, venue and soft score from the accept event into the row and the CSV', async () => {
    const h = harness();
    h.accept({ feedSource: 'laserstream', venue: 'pumpswap', entrySoftScore: 72 });
    await flush();
    h.advance(1000);
    h.tracker.injectTick(MINT, ENTRY * 0.5, h.clock());
    await flush();
    const row = h.row();
    expect(row.feed_source).toBe('laserstream');
    expect(row.venue).toBe('pumpswap');
    expect(row.entry_soft_score).toBe(72);
    expect(row.exit_trigger_to_confirm_ms).toBe(0); // poll tick: decision on the tick itself
    const csv = buildTradeBlotterCsv(h.db, { track: 'dry', range: 'all' });
    expect(csv).toMatch(/laserstream/);
    expect(csv).toMatch(/pumpswap/);
    h.tracker.stop();
  });
});

describe('S1 — experiment lane', () => {
  it('runs the dead-money variant in the twin only and stamps the override set on the row', async () => {
    const h = harness({ dryRunTwin: { exitOverrides: { deadMoneyEnabled: true, deadMoneyMinutes: 3, deadMoneyMaxMfePct: 5 } } });
    h.accept();
    await flush();
    h.advance(60_000);
    h.tracker.injectTick(MINT, ENTRY * 1.02, h.clock());
    h.advance(2 * 60_000 + 1);
    h.tracker.injectTick(MINT, ENTRY * 1.01, h.clock());
    await flush();
    const row = h.row();
    expect(row.state).toBe('CLOSED');
    expect(row.exit_reason).toBe('TIME_STOP');
    expect(JSON.parse(row.exit_overrides_json!)).toEqual({ deadMoneyEnabled: true, deadMoneyMinutes: 3, deadMoneyMaxMfePct: 5 });
    h.tracker.stop();
  });

  it('leaves exit_overrides_json null on the baseline', async () => {
    const h = harness();
    h.accept();
    await flush();
    h.advance(1000);
    h.tracker.injectTick(MINT, ENTRY * 0.5, h.clock());
    await flush();
    expect(h.row().exit_overrides_json).toBeNull();
    h.tracker.stop();
  });
});
