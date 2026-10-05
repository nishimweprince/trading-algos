import { describe, expect, it } from 'vitest';
import { simulateEquity, type EquityPosition } from '../src/racer/equity.ts';
import { openDb } from '../src/persistence/db.ts';
import { getRacerSummary } from '../src/dashboard/queries.ts';

function pos(mint: string, entryAtMs: number, exitAtMs: number, sizeSol: number, pnlSol: number): EquityPosition {
  return { mint, entryAtMs, exitAtMs, sizeSol, pnlSol };
}

describe('simulateEquity', () => {
  it('accumulates realized pnl and emits the curve in exit-time order', () => {
    const r = simulateEquity(
      [pos('B', 200, 300, 1, -0.05), pos('A', 0, 100, 1, 0.1)],
      { startBalanceSol: 10, rentSol: 0.01, sweepDelayMs: 1_000 },
    );
    expect(r.taken).toBe(2);
    expect(r.skippedCapital).toBe(0);
    expect(r.skippedConcurrent).toBe(0);
    expect(r.finalEquitySol).toBeCloseTo(10.05, 12);
    expect(r.curve.map((p) => p.mint)).toEqual(['A', 'B']);
    expect(r.curve[1]!.equitySol).toBeCloseTo(10.05, 12);
    // A's rent is still locked when B enters (slow sweeper), so the peak carries it.
    expect(r.peakLockedSol).toBeCloseTo(1.02, 12);
  });

  it('skips positions the free balance cannot cover, then resumes', () => {
    const r = simulateEquity(
      [pos('A', 0, 100, 5, 1), pos('B', 200, 300, 0.5, 0.05)],
      { startBalanceSol: 1, rentSol: 0.01, sweepDelayMs: 1_000 },
    );
    expect(r.taken).toBe(1);
    expect(r.skippedCapital).toBe(1);
    expect(r.finalEquitySol).toBeCloseTo(1.05, 12);
  });

  it('caps overlapping positions at maxConcurrent', () => {
    const r = simulateEquity(
      [pos('A', 0, 1_000, 1, 0), pos('B', 100, 1_100, 1, 0), pos('C', 2_000, 2_100, 1, 0)],
      { startBalanceSol: 10, maxConcurrent: 1, rentSol: 0, sweepDelayMs: 0 },
    );
    expect(r.taken).toBe(2);
    expect(r.skippedConcurrent).toBe(1);
    expect(r.curve.map((p) => p.mint)).toEqual(['A', 'C']);
  });

  it('frees rent at the sweeper cadence — a slow sweeper starves the next entry', () => {
    const ps = [pos('A', 0, 100, 1, 0), pos('B', 200, 300, 1, 0)];
    const slow = simulateEquity(ps, { startBalanceSol: 1.01, rentSol: 0.01, sweepDelayMs: 1_000 });
    expect(slow.taken).toBe(1);
    expect(slow.skippedCapital).toBe(1);
    // The skipped position's phantom rent release must not mint money.
    expect(slow.finalEquitySol).toBeCloseTo(1.01, 12);
    const fast = simulateEquity(ps, { startBalanceSol: 1.01, rentSol: 0.01, sweepDelayMs: 50 });
    expect(fast.taken).toBe(2);
    expect(fast.skippedCapital).toBe(0);
  });

  it('tracks peak equity and max drawdown over exits', () => {
    const r = simulateEquity(
      [pos('A', 0, 100, 1, 1), pos('B', 200, 300, 1, -3), pos('C', 400, 500, 1, 1)],
      { startBalanceSol: 10, rentSol: 0, sweepDelayMs: 0 },
    );
    expect(r.peakEquitySol).toBeCloseTo(11, 12);
    expect(r.maxDrawdownSol).toBeCloseTo(3, 12);
    expect(r.maxDrawdownPct).toBeCloseTo((3 / 11) * 100, 12);
  });

  it('frees same-instant exits before entries, so back-to-back trades fit', () => {
    const r = simulateEquity(
      [pos('A', 0, 100, 1, 0), pos('B', 100, 200, 0.99, 0)],
      { startBalanceSol: 1.01, rentSol: 0.01, sweepDelayMs: 1_000_000 },
    );
    // B needs A's freed size (A's rent stays locked); entry-before-exit would starve it.
    expect(r.taken).toBe(2);
    expect(r.skippedCapital).toBe(0);
  });
});

describe('racer summary equity', () => {
  function seed(withBalances: boolean): ReturnType<typeof openDb> {
    const db = openDb({ path: ':memory:', memory: true });
    const horizons = JSON.stringify([{ sec: 60, pnl: { slot1mid: 5 }, exitPrice: 1, exitQuoteSol: 10 }]);
    const entries = JSON.stringify({ slot1mid: { price: 1, quoteSol: 10 } });
    const ins = db.prepare(`INSERT INTO racer_paper_tracks
      (mint, size_sol, reach_price, reach_quote_sol, horizons_json, entries_json, created_at, armed,
       entry_model_version, entry_quality, live_cost_sol, live_cost_source, wallet_balance_sol)
      VALUES (?, 0.1, 1, 10, ?, ?, ?, 1, 2, 'verified', 0.001, 'measured', ?)`);
    ins.run('A', horizons, entries, '2026-01-01 00:00:00', withBalances ? 1.0 : null);
    ins.run('B', horizons, entries, '2026-01-01 00:01:05', withBalances ? 0.9 : null);
    return db;
  }

  it('walks the horizon exits from the earliest real wallet balance', () => {
    const db = seed(true);
    const equity = getRacerSummary(db, { ataRentSol: 0.002 }).paper.horizons.find((h) => h.sec === 60)!.equity!;
    expect(equity.startBalanceSol).toBe(1.0);
    expect(equity.taken).toBe(2);
    expect(equity.curve).toHaveLength(2);
    expect(equity.finalEquitySol).toBeCloseTo(1.01, 9);
    expect(equity.maxDrawdownSol).toBe(0);
  });

  it('is null when no track carries a wallet balance', () => {
    const db = seed(false);
    expect(getRacerSummary(db, {}).paper.horizons.find((h) => h.sec === 60)!.equity).toBeNull();
  });
});
