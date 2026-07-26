import { describe, expect, it } from 'vitest';
import { openDb, type DB } from '../src/persistence/db.ts';
import { Repositories, type DryRunPositionRow } from '../src/persistence/repositories.ts';
import { ConfigSchema } from '../src/config/schema.ts';
import { TypedBus } from '../src/core/bus.ts';
import { DryRunTracker } from '../src/positions/dryRunTracker.ts';
import type { RpcClient } from '../src/core/rpc.ts';
import type { PoolPricingRef } from '../src/core/types.ts';
import {
  buildExecutionDragCsv,
  buildTradeBlotterCsv,
  getDashboardSummary,
  getExecutionDragComparison,
  getFunnelAnalytics,
  getPerformanceAnalytics,
  getPnlSeries,
  getRelaxedRiskAnalytics,
  listPositions,
} from '../src/dashboard/queries.ts';
import { buildHourlySnapshot } from '../src/dashboard/analytics.ts';
import { buildStrategyWeekReport } from '../src/dashboard/strategyReport.ts';

const CFG = ConfigSchema.parse({});

/**
 * Seed `dry_run_positions` with rows that would be catastrophic if they ever
 * leaked into `positions` reads:
 *   - a huge realized LOSS (would trip the daily-loss limit / consecutive-loss halt)
 *   - an EMERGENCY_EXIT close (would trip the 24h emergency-exit breaker)
 *   - an OPEN and an EXITING-shaped row (would be resurrected by crash recovery)
 */
function seedDryRows(repos: Repositories, now = Date.now()): void {
  repos.upsertDryRunPosition({
    mint: 'dryLoser',
    state: 'CLOSED',
    liveStatus: 'blocked_concurrent',
    sizeSol: 0.03,
    entryPrice: 1,
    exitPrice: 0.2,
    exitReason: 'EMERGENCY_EXIT',
    openedAt: now - 60_000,
    closedAt: now,
    grossPnlSol: -900,
    feesSol: 0.002,
    netPnlSol: -900,
    pnlPct: -80,
    holdMs: 60_000,
    fillCount: 1,
    samples: 12,
    relaxedRisk: true,
    mode: 'live',
  });
  repos.upsertDryRunPosition({
    mint: 'dryOpen',
    state: 'OPEN',
    liveStatus: 'entered',
    sizeSol: 0.03,
    entryPrice: 1,
    openedAt: now - 5_000,
    mode: 'live',
  });
}

/** A single real live trade, so "excludes dry" is distinguishable from "reads nothing". */
function seedOneLiveTrade(repos: Repositories, now = Date.now()): void {
  repos.upsertPosition(
    {
      mint: 'liveWinner',
      state: 'CLOSED',
      sizeSol: 0.03,
      entryPrice: 1,
      openedAt: now - 60_000,
      closedAt: now,
      pnlSol: 0.01,
      pnlPct: 33,
    },
    { netPnlSol: 0.01, grossPnlSol: 0.011, feesSol: 0.001, mode: CFG.mode, exitPrice: 1.4 },
  );
}

function fresh(): { db: DB; repos: Repositories } {
  const db = openDb({ path: ':memory:', memory: true });
  return { db, repos: new Repositories(db) };
}

/**
 * THE load-bearing invariant of the dry-run twin feature.
 *
 * `positions` is read UNFILTERED by the kill switch, daily-loss limit,
 * consecutive-loss halt and crash recovery. If a simulated row could reach any
 * of those, simulated losses would halt real trading — or worse, crash recovery
 * would drive a real exit send path for a mint the wallet never held.
 *
 * Keeping the twin in its own table makes that structurally impossible. This
 * suite is the enumeration that proves it, across every category of read site.
 */
describe('dry-run twin isolation from live reads', () => {
  describe('risk-critical repository reads (kill switch / breakers / recovery)', () => {
    it.each([
      ['sumRealizedPnlSince', (r: Repositories) => r.sumRealizedPnlSince(new Date(0).toISOString()), 0],
      [
        'countClosedByTriggerSince',
        (r: Repositories) => r.countClosedByTriggerSince('EMERGENCY_EXIT', new Date(0).toISOString()),
        0,
      ],
      ['recentClosedPnls', (r: Repositories) => r.recentClosedPnls(10).length, 0],
      ['recentClosedPnlRecords', (r: Repositories) => r.recentClosedPnlRecords(10).length, 0],
      ['latestOpenPositions', (r: Repositories) => r.latestOpenPositions().length, 0],
      ['latestExitingPositions', (r: Repositories) => r.latestExitingPositions().length, 0],
    ])('%s ignores dry-run rows', (_name, read, expected) => {
      const { repos } = fresh();
      seedDryRows(repos);
      expect(read(repos)).toBe(expected);
    });

    it('sees live rows while ignoring dry rows', () => {
      const { repos } = fresh();
      seedDryRows(repos);
      seedOneLiveTrade(repos);
      expect(repos.sumRealizedPnlSince(new Date(0).toISOString())).toBeCloseTo(0.01, 6);
      expect(repos.recentClosedPnls(10)).toEqual([0.01]);
      expect(repos.countClosedByTriggerSince('EMERGENCY_EXIT', new Date(0).toISOString())).toBe(0);
    });
  });

  describe('dashboard + analytics reads', () => {
    it('summary PnL, win rate and exposure exclude dry rows', () => {
      const { db, repos } = fresh();
      seedDryRows(repos);
      const summary = getDashboardSummary(db, CFG);
      expect(summary.pnl.realizedSol).toBe(0);
      expect(summary.pnl.closedCount).toBe(0);
      expect(summary.pnl.unrealizedSol).toBe(0);
      expect(summary.positions.openCount).toBe(0);
      expect(summary.positions.openExposureSol).toBe(0);
    });

    it('pnl series, positions list and performance stats exclude dry rows', () => {
      const { db, repos } = fresh();
      seedDryRows(repos);
      expect(getPnlSeries(db, { range: '30d' })).toHaveLength(0);
      expect(listPositions(db, { state: 'all' })).toHaveLength(0);
      expect(listPositions(db, { state: 'open' })).toHaveLength(0);
      expect(listPositions(db, { state: 'closed' })).toHaveLength(0);
      expect(getPerformanceAnalytics(db, { range: 'all' }).tradeCount).toBe(0);
    });

    it('funnel and relaxed-risk analytics exclude dry rows', () => {
      const { db, repos } = fresh();
      seedDryRows(repos);
      const funnel = getFunnelAnalytics(db, { range: 'all' });
      expect(funnel.entered).toBe(0);
      expect(funnel.closed).toBe(0);
      expect(funnel.failed).toBe(0);
      const relaxed = getRelaxedRiskAnalytics(db, { range: 'all' });
      for (const group of relaxed.groups) expect(group.netPnlSol).toBe(0);
    });

    it('trade blotter CSV emits headers only', () => {
      const { db, repos } = fresh();
      seedDryRows(repos);
      const lines = buildTradeBlotterCsv(db, { range: 'all' }).trim().split('\n');
      expect(lines).toHaveLength(1);
      expect(lines[0]).toContain('mint');
    });

    it('hourly analytics snapshots exclude dry rows', () => {
      const { db, repos } = fresh();
      const periodStart = new Date(Date.now() - 1_800_000).toISOString();
      seedDryRows(repos);
      buildHourlySnapshot(db, CFG, periodStart);
      const row = db
        .prepare(`SELECT realized_pnl_sol AS pnl, trade_count AS n FROM analytics_snapshots LIMIT 1`)
        .get() as { pnl: number; n: number } | undefined;
      expect(row?.pnl ?? 0).toBe(0);
      expect(row?.n ?? 0).toBe(0);
    });

    it('strategy-week report excludes dry rows', () => {
      const { db, repos } = fresh();
      seedDryRows(repos);
      const report = buildStrategyWeekReport(db, CFG, { range: 'all', allowMixedModes: true });
      expect(report.headline.tradeCount).toBe(0);
      expect(report.headline.realizedPnlSol).toBe(0);
      expect(report.headline.failedCount).toBe(0);
      expect(report.examples.worstTrades).toHaveLength(0);
    });
  });
});

describe('dry-run twin persistence', () => {
  it('is append-only, so latestDryRunPositions returns the newest row per mint', () => {
    const { repos } = fresh();
    const now = Date.now();
    repos.upsertDryRunPosition({
      mint: 'm1',
      state: 'OPEN',
      liveStatus: 'pending_entry',
      sizeSol: 0.03,
      entryPrice: 1,
      openedAt: now,
    });
    repos.upsertDryRunPosition({
      mint: 'm1',
      state: 'CLOSED',
      liveStatus: 'entered',
      sizeSol: 0.03,
      entryPrice: 1,
      exitPrice: 1.5,
      exitReason: 'TAKE_PROFIT_1',
      openedAt: now,
      closedAt: now + 30_000,
      netPnlSol: 0.014,
      holdMs: 30_000,
    });

    const all = repos.latestDryRunPositions();
    expect(all).toHaveLength(1);
    expect(all[0]!.state).toBe('CLOSED');
    expect(all[0]!.live_status).toBe('entered');
    expect(all[0]!.exit_reason).toBe('TAKE_PROFIT_1');
    // pnl_sol mirrors net so the row stays shape-compatible with `positions`.
    expect(all[0]!.pnl_sol).toBeCloseTo(0.014, 6);
    expect(all[0]!.net_pnl_sol).toBeCloseTo(0.014, 6);
  });

  it('stores timestamps as ISO text so range predicates match the positions table', () => {
    const { db, repos } = fresh();
    const now = Date.now();
    repos.upsertDryRunPosition({
      mint: 'm1',
      state: 'CLOSED',
      liveStatus: 'entered',
      openedAt: now - 1000,
      closedAt: now,
      netPnlSol: 0.01,
    });
    const row = db
      .prepare(
        `SELECT COUNT(*) AS n FROM dry_run_positions
         WHERE julianday(closed_at) >= julianday('now', '-1 day')`,
      )
      .get() as { n: number };
    expect(row.n).toBe(1);
  });

  it('records coverage events so bounded twin coverage is never silent', () => {
    const { db, repos } = fresh();
    repos.recordDryRunCoverage('eligible', 'm1');
    repos.recordDryRunCoverage('dropped_capacity', 'm2', 'cap=8');
    const rows = db
      .prepare(`SELECT kind, mint, detail FROM dry_run_coverage_events ORDER BY rowid`)
      .all() as Array<{ kind: string; mint: string | null; detail: string | null }>;
    expect(rows).toEqual([
      { kind: 'eligible', mint: 'm1', detail: null },
      { kind: 'dropped_capacity', mint: 'm2', detail: 'cap=8' },
    ]);
  });
});

// ---------------------------------------------------------------------------
// Tracker behaviour
// ---------------------------------------------------------------------------

/**
 * Any property access beyond a plain account read is a bug: it would mean the
 * twin reached for an executor, a wallet, or a send path. The Proxy turns that
 * into a loud failure instead of a silent one.
 */
const paranoidRpc = new Proxy(
  {},
  {
    get(_t, prop: string) {
      if (prop === 'getMultipleAccountsBase64') return async () => [];
      if (prop === 'then') return undefined; // not a thenable
      throw new Error(`dry-run twin touched rpc.${prop} — it must only read accounts`);
    },
  },
) as unknown as RpcClient;

const twinPricing = (): PoolPricingRef => ({
  poolAddress: 'pool',
  baseMint: 'mint',
  baseVault: 'base-vault',
  quoteVault: 'quote-vault',
  baseDecimals: 6,
  baseReserve: 10n ** 15n, // 1e9 tokens
  quoteReserveLamports: 100n * 10n ** 9n, // 100 SOL -> mid 1e-7
});

const ENTRY_PRICE = 1e-7;
const flush = () => new Promise((resolve) => setTimeout(resolve, 0));

function trackerHarness(overrides: Record<string, unknown> = {}) {
  const bus = new TypedBus();
  const db = openDb({ path: ':memory:', memory: true });
  const repos = new Repositories(db);
  const config = ConfigSchema.parse({ mode: 'live', rpc: { primaryHttp: 'http://x' }, ...overrides });
  let clock = 0;
  const tracker = new DryRunTracker({ config, bus, repos, rpc: paranoidRpc, now: () => clock });
  return {
    bus,
    db,
    repos,
    tracker,
    advance: (ms: number) => {
      clock += ms;
    },
    accept: (mint: string, sizeSol = 0.03) =>
      bus.emit('openPosition', { mint, sizeSol, highVolatility: false, pricing: twinPricing() }),
    rows: () => repos.latestDryRunPositions(),
    row: (mint: string) => repos.latestDryRunPositions().find((r) => r.mint === mint) as DryRunPositionRow | undefined,
    coverage: () =>
      db.prepare(`SELECT kind, mint FROM dry_run_coverage_events ORDER BY rowid`).all() as Array<{
        kind: string;
        mint: string;
      }>,
  };
}

describe('DryRunTracker', () => {
  it('opens a twin at the pool mid and closes it on its own exit FSM', async () => {
    const h = trackerHarness();
    h.tracker.start();
    h.accept('M');
    await flush();

    expect(h.tracker.size).toBe(1);
    const open = h.row('M')!;
    expect(open.state).toBe('OPEN');
    // Mid price from the event's reserves — the same value openLive estimates.
    expect(open.entry_price).toBeCloseTo(ENTRY_PRICE, 12);

    // Drive it past the hard stop.
    h.advance(1000);
    h.tracker.injectTick('M', ENTRY_PRICE * 0.5, 1000);
    await flush();

    expect(h.tracker.size).toBe(0);
    const closed = h.row('M')!;
    expect(closed.state).toBe('CLOSED');
    expect(closed.exit_reason).toBe('STOP_LOSS');
    expect(closed.net_pnl_sol!).toBeLessThan(0);
    expect(closed.mae_pct!).toBeCloseTo(-50, 5);
    h.tracker.stop();
  });

  it('never reaches a send path — only account reads', async () => {
    const h = trackerHarness();
    h.tracker.start();
    h.accept('M');
    await flush();
    h.advance(500);
    h.tracker.injectTick('M', ENTRY_PRICE * 1.4, 500);
    h.advance(500);
    h.tracker.injectTick('M', ENTRY_PRICE * 0.5, 1000);
    await flush();
    // The Proxy would have thrown on any executor/wallet-shaped access.
    expect(h.row('M')!.state).toBe('CLOSED');
    h.tracker.stop();
  });

  it('opens a twin even when live blocks the entry, and attributes the block', async () => {
    const h = trackerHarness();
    h.tracker.start();
    // Registered AFTER the tracker: the normal in-process ordering.
    h.bus.on('openPosition', (e) =>
      h.bus.emit('entryVetoed', {
        mint: e.mint,
        reason: 'CIRCUIT_BREAKER',
        detail: 'max concurrent positions',
        code: 'MAX_CONCURRENT',
      }),
    );

    h.accept('M');
    await flush();
    h.advance(1000);
    h.tracker.injectTick('M', ENTRY_PRICE * 0.5, 1000);
    await flush();

    const row = h.row('M')!;
    expect(row.state).toBe('CLOSED');
    expect(row.live_status).toBe('blocked_concurrent');
    expect(row.live_status_detail).toBe('max concurrent positions');
    // Opportunity cost is measurable — the row is not a stub.
    expect(row.net_pnl_sol).not.toBeNull();
    h.tracker.stop();
  });

  /**
   * The race that actually bites: canStartEntry re-emits entryVetoed
   * synchronously, before its first await. If the twin were registered second,
   * the block would fire before the twin state existed and 100% of blocked
   * entries would be mis-attributed as `unknown`.
   */
  it('attributes a block that fires BEFORE the twin has seen the accept', async () => {
    const h = trackerHarness();
    // Deliberately registered first, so the veto lands before the twin opens.
    h.bus.on('openPosition', (e) =>
      h.bus.emit('entryVetoed', { mint: e.mint, reason: 'KILL_SWITCH', detail: 'engaged', code: 'KILL_SWITCH' }),
    );
    h.tracker.start();

    h.accept('M');
    await flush();
    h.advance(1000);
    h.tracker.injectTick('M', ENTRY_PRICE * 0.5, 1000);
    await flush();

    expect(h.row('M')!.live_status).toBe('blocked_kill_switch');
    h.tracker.stop();
  });

  it('upgrades pending_entry to entry_failed, and never rewrites a terminal status', async () => {
    const h = trackerHarness();
    h.tracker.start();
    h.accept('M');
    await flush();

    h.bus.emit('positionUpdate', { mint: 'M', state: 'PENDING_ENTRY', sizeSol: 0.03 });
    h.bus.emit('positionUpdate', { mint: 'M', state: 'FAILED', sizeSol: 0.03 });
    // A late, contradictory signal must not overwrite the terminal outcome.
    h.bus.emit('positionUpdate', { mint: 'M', state: 'OPEN', sizeSol: 0.03 });

    h.advance(1000);
    h.tracker.injectTick('M', ENTRY_PRICE * 0.5, 1000);
    await flush();

    expect(h.row('M')!.live_status).toBe('entry_failed');
    h.tracker.stop();
  });

  it('marks a successful live entry as entered', async () => {
    const h = trackerHarness();
    h.tracker.start();
    h.accept('M');
    await flush();
    h.bus.emit('positionUpdate', { mint: 'M', state: 'OPEN', sizeSol: 0.03, entryPrice: ENTRY_PRICE });
    h.advance(1000);
    h.tracker.injectTick('M', ENTRY_PRICE * 0.5, 1000);
    await flush();
    expect(h.row('M')!.live_status).toBe('entered');
    h.tracker.stop();
  });

  it('ignores pre-accept GUARDRAIL vetoes, which have no twin', async () => {
    const h = trackerHarness();
    h.tracker.start();
    h.accept('M');
    await flush();
    // No `code` — this is the pre-accept veto path, handled by ShadowTracker.
    h.bus.emit('entryVetoed', { mint: 'M', reason: 'GUARDRAIL', detail: 'H7' });
    h.advance(1000);
    h.tracker.injectTick('M', ENTRY_PRICE * 0.5, 1000);
    await flush();
    expect(h.row('M')!.live_status).toBe('unknown');
    h.tracker.stop();
  });

  it('caps concurrency and records every drop, so coverage is never silently bounded', async () => {
    const h = trackerHarness({ dryRunTwin: { maxConcurrent: 1 } });
    h.tracker.start();
    h.accept('A');
    h.accept('B');
    await flush();

    expect(h.tracker.size).toBe(1);
    expect(h.coverage()).toContainEqual({ kind: 'dropped_capacity', mint: 'B' });
    h.tracker.stop();
  });

  it('sweeps twins whose pool stopped ticking, force-closing at the last price', async () => {
    const h = trackerHarness({ dryRunTwin: { windowMinutes: 1 } });
    h.tracker.start();
    h.accept('M');
    await flush();
    h.tracker.injectTick('M', ENTRY_PRICE * 1.05, 100);

    // No further ticks; the window simply expires.
    h.advance(61_000);
    h.tracker.stop(); // stop() flushes, exercising the same finish path

    const row = h.row('M')!;
    expect(row.state).toBe('CLOSED');
    expect(row.exit_reason).toBeTruthy();
    expect(row.hold_ms).not.toBeNull();
  });

  it('mirrors the live size so the delta reads directly as SOL cost', async () => {
    const h = trackerHarness();
    h.tracker.start();
    h.accept('M', 0.017);
    await flush();
    expect(h.row('M')!.size_sol).toBeCloseTo(0.017, 6);
    h.tracker.stop();
  });

  it('skips a candidate it cannot price rather than inventing an entry', async () => {
    const h = trackerHarness();
    h.tracker.start();
    h.bus.emit('openPosition', {
      mint: 'M',
      sizeSol: 0.03,
      highVolatility: false,
      pricing: { ...twinPricing(), baseReserve: 0n, quoteReserveLamports: 0n },
    });
    await flush();
    await flush();

    expect(h.tracker.size).toBe(0);
    expect(h.rows()).toHaveLength(0);
    expect(h.coverage()).toContainEqual({ kind: 'skipped_missing_pricing', mint: 'M' });
    h.tracker.stop();
  });

  it('does not open twins when disabled', async () => {
    const h = trackerHarness({ dryRunTwin: { enabled: false } });
    h.tracker.start();
    h.accept('M');
    await flush();
    expect(h.tracker.size).toBe(0);
    expect(h.rows()).toHaveLength(0);
    h.tracker.stop();
  });

  it('records no_executor outside live mode, where there is no live leg to compare', async () => {
    const bus = new TypedBus();
    const repos = new Repositories(openDb({ path: ':memory:', memory: true }));
    const config = ConfigSchema.parse({ mode: 'paper' });
    let clock = 0;
    const tracker = new DryRunTracker({ config, bus, repos, rpc: paranoidRpc, now: () => clock });
    tracker.start();
    bus.emit('openPosition', { mint: 'M', sizeSol: 0.03, highVolatility: false, pricing: twinPricing() });
    await flush();
    clock += 1000;
    tracker.injectTick('M', ENTRY_PRICE * 0.5, 1000);
    await flush();

    expect(repos.latestDryRunPositions()[0]!.live_status).toBe('no_executor');
    tracker.stop();
  });
});

// ---------------------------------------------------------------------------
// Execution drag (live vs dry) aggregation
// ---------------------------------------------------------------------------

describe('getExecutionDragComparison', () => {
  /**
   * Hand-computable numbers.
   *   live: entry 1.10, exit 1.30, net -0.010, hold 9000ms
   *   dry:  entry 1.00, exit 1.50, net +0.030, hold 5000ms
   * so entryDrag = +10%, exitDrag = -13.33%, netDelta = -0.040, holdDelta = +4000
   */
  function seedPair(repos: Repositories, now = Date.now()) {
    repos.upsertPosition(
      {
        mint: 'PAIR',
        state: 'CLOSED',
        sizeSol: 0.03,
        entryPrice: 1.1,
        openedAt: now - 9_000,
        closedAt: now,
        exitTrigger: 'STOP_LOSS',
        pnlSol: -0.01,
        pnlPct: -33.3,
      },
      { netPnlSol: -0.01, feesSol: 0.004, exitPrice: 1.3, holdMs: 9_000, mfePct: 30, exitTriggerToConfirmMs: 2_400 },
    );
    repos.upsertDryRunPosition({
      mint: 'PAIR',
      state: 'CLOSED',
      liveStatus: 'entered',
      sizeSol: 0.03,
      entryPrice: 1.0,
      exitPrice: 1.5,
      exitReason: 'TAKE_PROFIT_1',
      openedAt: now - 5_000,
      closedAt: now,
      netPnlSol: 0.03,
      pnlPct: 100,
      feesSol: 0.002,
      mfePct: 50,
      holdMs: 5_000,
      fillCount: 1,
    });
  }

  it('computes per-trade drag for a mint both legs closed', () => {
    const { db, repos } = fresh();
    seedPair(repos);
    const cmp = getExecutionDragComparison(db, { range: 'all' });

    expect(cmp.cohorts.bothN).toBe(1);
    const row = cmp.rows[0]!;
    expect(row.cohort).toBe('both');
    expect(row.entryDragPct).toBeCloseTo(10, 6);
    expect(row.exitDragPct).toBeCloseTo(-13.3333, 3);
    expect(row.netPnlDeltaSol).toBeCloseTo(-0.04, 9);
    expect(row.holdMsDelta).toBe(4_000);
    expect(row.feeDeltaSol).toBeCloseTo(0.002, 9);
    expect(row.mfeDeltaPct).toBeCloseTo(-20, 6);
    expect(row.exitConfirmMs).toBe(2_400);
    // Live stopped out where dry took profit — the exit paths diverged.
    expect(row.exitTriggerMatch).toBe(false);

    expect(cmp.totals.netPnlDeltaSol).toBeCloseTo(-0.04, 9);
    expect(cmp.perTrade.avgNetPnlDeltaSol).toBeCloseTo(-0.04, 9);
    expect(cmp.perTrade.medianHoldMsDelta).toBe(4_000);
    expect(cmp.perTrade.exitTriggerMatchPct).toBe(0);
    expect(cmp.winRate.livePct).toBe(0);
    expect(cmp.winRate.dryPct).toBe(100);
  });

  it('books blocked accepts as opportunity cost, not as drag', () => {
    const { db, repos } = fresh();
    const now = Date.now();
    repos.upsertDryRunPosition({
      mint: 'BLOCKED',
      state: 'CLOSED',
      liveStatus: 'blocked_concurrent',
      sizeSol: 0.03,
      entryPrice: 1,
      exitPrice: 1.6,
      exitReason: 'TAKE_PROFIT_1',
      openedAt: now - 5_000,
      closedAt: now,
      netPnlSol: 0.018,
      holdMs: 5_000,
    });

    const cmp = getExecutionDragComparison(db, { range: 'all' });
    expect(cmp.cohorts).toMatchObject({ bothN: 0, dryOnlyN: 1, liveOnlyN: 0 });
    // Excluded from drag (there is no live leg to compare against)...
    expect(cmp.totals.netPnlDeltaSol).toBe(0);
    // ...but counted as what the concurrency cap cost.
    expect(cmp.totals.opportunityCostSol).toBeCloseTo(0.018, 9);
    expect(cmp.byLiveStatus).toContainEqual(
      expect.objectContaining({ liveStatus: 'blocked_concurrent', n: 1 }),
    );
  });

  it('treats a live FAILED entry as dry_only, since capital never went in', () => {
    const { db, repos } = fresh();
    const now = Date.now();
    repos.upsertPosition(
      { mint: 'F', state: 'FAILED', sizeSol: 0.03, entryPrice: 1, openedAt: now - 1_000 },
      { mode: 'live' },
    );
    repos.upsertDryRunPosition({
      mint: 'F',
      state: 'CLOSED',
      liveStatus: 'entry_failed',
      sizeSol: 0.03,
      entryPrice: 1,
      exitPrice: 0.8,
      openedAt: now - 1_000,
      closedAt: now,
      netPnlSol: -0.006,
    });

    const cmp = getExecutionDragComparison(db, { range: 'all' });
    expect(cmp.cohorts.bothN).toBe(0);
    expect(cmp.cohorts.dryOnlyN).toBe(1);
    expect(cmp.rows[0]!.liveStatus).toBe('entry_failed');
  });

  it('surfaces a live close with no twin as a coverage gap rather than hiding it', () => {
    const { db, repos } = fresh();
    seedOneLiveTrade(repos);
    const cmp = getExecutionDragComparison(db, { range: 'all' });
    expect(cmp.cohorts.liveOnlyN).toBe(1);
    expect(cmp.cohorts.bothN).toBe(0);
    expect(cmp.rows[0]!.cohort).toBe('live_only');
  });

  it('counts in-flight legs as incomplete instead of aggregating them', () => {
    const { db, repos } = fresh();
    const now = Date.now();
    repos.upsertDryRunPosition({
      mint: 'OPEN1',
      state: 'OPEN',
      liveStatus: 'entered',
      sizeSol: 0.03,
      entryPrice: 1,
      openedAt: now,
    });
    repos.upsertPosition({ mint: 'OPEN2', state: 'OPEN', sizeSol: 0.03, entryPrice: 1, openedAt: now });

    const cmp = getExecutionDragComparison(db, { range: 'all' });
    expect(cmp.cohorts.incompleteN).toBe(2);
    expect(cmp.cohorts.bothN).toBe(0);
    expect(cmp.rows).toHaveLength(0);
  });

  it('scopes to a session when asked, and to everything when not', () => {
    const { db, repos } = fresh();
    seedPair(repos);
    // The seeded rows have no session_id, so session scoping must exclude them.
    expect(getExecutionDragComparison(db, { range: 'all', sessionId: 7 }).cohorts.bothN).toBe(0);
    expect(getExecutionDragComparison(db, { range: 'all', sessionId: 7, allSessions: true }).cohorts.bothN).toBe(1);
  });

  it('exports a CSV whose header warns that fees are estimated', () => {
    const { db, repos } = fresh();
    seedPair(repos);
    const lines = buildExecutionDragCsv(db, { range: 'all' }).trim().split('\n');
    expect(lines[0]).toContain('estimated on both legs');
    expect(lines[1]).toContain('net_pnl_delta_sol');
    expect(lines[2]).toContain('PAIR');
    expect(lines[2]).toContain('both');
  });

  it('reports an empty comparison rather than throwing when there is no data', () => {
    const { db } = fresh();
    const cmp = getExecutionDragComparison(db, { range: '7d' });
    expect(cmp.cohorts).toEqual({ bothN: 0, dryOnlyN: 0, liveOnlyN: 0, incompleteN: 0, unknownStatusN: 0 });
    expect(cmp.perTrade.avgNetPnlDeltaSol).toBeNull();
    expect(cmp.rows).toHaveLength(0);
  });
});

describe('track-scoped dashboard reads', () => {
  it('serves summary, positions and PnL from whichever table the track names', () => {
    const { db, repos } = fresh();
    const now = Date.now();
    seedOneLiveTrade(repos, now);
    repos.upsertDryRunPosition({
      mint: 'dryOnly',
      state: 'CLOSED',
      liveStatus: 'blocked_breaker',
      sizeSol: 0.03,
      entryPrice: 1,
      exitPrice: 1.9,
      openedAt: now - 1_000,
      closedAt: now,
      netPnlSol: 0.027,
      pnlPct: 90,
    });

    const live = getDashboardSummary(db, CFG, { track: 'live' });
    const dry = getDashboardSummary(db, CFG, { track: 'dry' });
    expect(live.pnl.realizedSol).toBeCloseTo(0.01, 9);
    expect(dry.pnl.realizedSol).toBeCloseTo(0.027, 9);

    expect(listPositions(db, { state: 'all', track: 'live' }).map((p) => p.mint)).toEqual(['liveWinner']);
    expect(listPositions(db, { state: 'all', track: 'dry' }).map((p) => p.mint)).toEqual(['dryOnly']);

    expect(getPnlSeries(db, { range: '30d', track: 'live' })).toHaveLength(1);
    expect(getPnlSeries(db, { range: '30d', track: 'dry' })).toHaveLength(1);
  });

  it('serves a cumulative delta series over mints both legs closed', () => {
    const { db, repos } = fresh();
    const now = Date.now();
    repos.upsertPosition(
      { mint: 'P', state: 'CLOSED', sizeSol: 0.03, entryPrice: 1, openedAt: now - 1_000, closedAt: now, pnlSol: -0.01 },
      { netPnlSol: -0.01 },
    );
    repos.upsertDryRunPosition({
      mint: 'P',
      state: 'CLOSED',
      liveStatus: 'entered',
      sizeSol: 0.03,
      entryPrice: 1,
      openedAt: now - 1_000,
      closedAt: now,
      netPnlSol: 0.02,
    });

    const series = getPnlSeries(db, { range: '30d', track: 'delta' });
    expect(series).toHaveLength(1);
    expect(series[0]!.cumulativePnlSol).toBeCloseTo(-0.03, 9);
  });

  it('never invents a mark-to-market for an open dry twin', () => {
    const { db, repos } = fresh();
    repos.upsertDryRunPosition({
      mint: 'M',
      state: 'OPEN',
      liveStatus: 'entered',
      sizeSol: 0.03,
      entryPrice: 1,
      openedAt: Date.now(),
    });
    // A live price tick exists for the same mint; the dry read must ignore it.
    db.prepare(`INSERT INTO price_ticks (mint, slot, price, sol_reserve) VALUES ('M', 1, 2.0, 100)`).run();

    expect(getDashboardSummary(db, CFG, { track: 'dry' }).pnl.unrealizedSol).toBe(0);
    expect(listPositions(db, { state: 'open', track: 'dry' })[0]!.unrealizedSol).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// Acceptance gate for the funded pilot
// ---------------------------------------------------------------------------

/**
 * In paper mode the "live" leg is ITSELF an ideal mid-price fill, so it and the
 * twin should agree exactly. That makes paper mode a self-test of the
 * instrumentation: a materially nonzero Δ there is a measurement bug, not
 * execution drag. Run this gate before trusting a live pilot's Δ.
 *
 * The one legitimate source of divergence is that openPaperLike takes a FRESH
 * vault read at open while the twin prices off the reserves carried on the
 * event. This test pins the no-fresh-read case, where the two must be identical
 * to the last decimal.
 */
describe('acceptance gate: paper mode Δ must be zero', () => {
  it('paper live and the twin agree exactly when both price off the same reserves', async () => {
    const bus = new TypedBus();
    const db = openDb({ path: ':memory:', memory: true });
    const repos = new Repositories(db);
    const config = ConfigSchema.parse({ mode: 'paper' });
    let clock = 0;
    const now = () => clock;

    const { PositionManager } = await import('../src/positions/manager.ts');
    type Tick = { mint: string; price: number; atMs: number; baseReserve: bigint; quoteReserveLamports: bigint };

    // readOnce -> null, so openPaperLike falls back to the event's reserves:
    // the exact same input the twin prices off.
    let feedLive: (t: Tick) => void = () => {};
    const livePoller = {
      setHandler: (h: (t: Tick) => void) => {
        feedLive = h;
      },
      register: () => {},
      unregister: () => {},
      readOnce: async () => null,
      start: () => {},
      stop: () => {},
      get size() {
        return 0;
      },
    };

    const tracker = new DryRunTracker({ config, bus, repos, rpc: paranoidRpc, now });
    const mgr = new PositionManager({
      config,
      bus,
      repos,
      poller: livePoller as unknown as ConstructorParameters<typeof PositionManager>[0]['poller'],
      now,
    });

    tracker.start(); // before positions, exactly as index.ts wires it
    mgr.start();

    bus.emit('openPosition', { mint: 'M', sizeSol: 0.03, highVolatility: false, pricing: twinPricing() });
    await flush();
    await flush();

    // Identical price path down both legs.
    for (const price of [1.3e-7, 1.6e-7, 0.9e-7]) {
      clock += 1000;
      feedLive({ mint: 'M', price, atMs: clock, baseReserve: 0n, quoteReserveLamports: 0n });
      tracker.injectTick('M', price, clock);
    }
    await flush();

    tracker.stop();
    mgr.stop();

    const cmp = getExecutionDragComparison(db, { range: 'all' });
    expect(cmp.cohorts.bothN).toBe(1);
    const row = cmp.rows[0]!;
    expect(row.entryDragPct).toBeCloseTo(0, 9);
    expect(row.netPnlDeltaSol).toBeCloseTo(0, 9);
    expect(row.holdMsDelta).toBe(0);
    expect(row.exitTriggerMatch).toBe(true);
    expect(cmp.totals.netPnlDeltaSol).toBeCloseTo(0, 9);
  });
});
