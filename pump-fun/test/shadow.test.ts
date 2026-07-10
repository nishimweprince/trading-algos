import { describe, expect, it } from 'vitest';
import { openDb } from '../src/persistence/db.ts';
import { Repositories } from '../src/persistence/repositories.ts';
import { getShadowVetoQuality, getVetoDryRunComparison } from '../src/dashboard/queries.ts';
import { ShadowTracker } from '../src/guardrails/shadow.ts';
import { estimatePaperFees } from '../src/positions/paperFees.ts';
import { ConfigSchema } from '../src/config/schema.ts';
import type { RpcClient } from '../src/core/rpc.ts';
import type { PoolRef } from '../src/positions/pricing.ts';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

// PricePoller only touches rpc while started; injectTick bypasses RPC.
const stubRpc = { getMultipleAccountsBase64: async () => [] } as unknown as RpcClient;

function poolRef(mint: string): PoolRef {
  return { mint, baseVault: `${mint}-base`, quoteVault: `${mint}-quote`, baseDecimals: 6 };
}

const CFG = ConfigSchema.parse({});

describe('shadow veto-quality aggregation', () => {
  it('groups counterfactual outcomes by primary veto reason', () => {
    const db = openDb({ path: ':memory:', memory: true });
    const repos = new Repositories(db);
    const base = {
      baselinePrice: 1,
      troughPrice: 0.9,
      maxMaePct: -10,
      samples: 5,
      trackedMs: 1200,
    };
    // Two H7 vetoes: one pumped +60% (would-be false positive), one flat.
    repos.recordShadowOutcome({
      ...base,
      mint: 'm1',
      verdict: 'veto',
      primaryVetoCode: 'H7',
      peakPrice: 1.6,
      peakMfePct: 60,
      hit25: true,
      hit50: true,
      netPnlSol: 0.04,
      exitReason: 'TAKE_PROFIT_1',
      holdMs: 60_000,
    });
    repos.recordShadowOutcome({
      ...base,
      mint: 'm2',
      verdict: 'veto',
      primaryVetoCode: 'H7',
      peakPrice: 1.05,
      peakMfePct: 5,
      hit25: false,
      hit50: false,
      netPnlSol: -0.02,
      exitReason: 'STOP_LOSS',
      holdMs: 30_000,
    });
    // One UNKNOWN:H4 veto that did nothing.
    repos.recordShadowOutcome({
      ...base,
      mint: 'm3',
      verdict: 'veto',
      primaryVetoCode: 'UNKNOWN:H4',
      peakPrice: 1.1,
      peakMfePct: 10,
      hit25: false,
      hit50: false,
      netPnlSol: -0.01,
      exitReason: 'TIME_STOP',
      holdMs: 900_000,
    });

    const q = getShadowVetoQuality(db, { range: 'all' });
    expect(q.totalTracked).toBe(3);
    const byReason = Object.fromEntries(q.byReason.map((r) => [r.primaryVetoCode, r]));
    expect(byReason['H7']!.n).toBe(2);
    expect(byReason['H7']!.avgPeakMfePct).toBeCloseTo(32.5);
    expect(byReason['H7']!.hit50Pct).toBeCloseTo(50); // 1 of 2 hit +50%
    expect(byReason['H7']!.avgNetPnlSol).toBeCloseTo(0.01); // (0.04 + -0.02) / 2
    expect(byReason['H7']!.winRatePct).toBeCloseTo(50);
    expect(byReason['UNKNOWN:H4']!.hit50Pct).toBe(0);
    db.close();
  });
});

describe('ShadowTracker registration guards', () => {
  it('dedupes, caps, and ignores unpriceable candidates', () => {
    const db = openDb({ path: ':memory:', memory: true });
    const repos = new Repositories(db);
    const tracker = new ShadowTracker(stubRpc, repos, { maxConcurrent: 2, sizeSol: 0.25 });

    const track = (mint: string, baselinePrice: number) =>
      tracker.track({ mint, verdict: 'veto', primaryVetoCode: 'H7', baselinePrice, poolRef: poolRef(mint) });

    track('a', 1);
    track('a', 1); // duplicate — ignored
    expect(tracker.size).toBe(1);

    track('b', 0); // baseline 0 — can't price, ignored
    expect(tracker.size).toBe(1);

    track('c', 2);
    expect(tracker.size).toBe(2);

    track('d', 2); // at capacity (2) — dropped
    expect(tracker.size).toBe(2);

    tracker.stop();
    db.close();
  });
});

describe('ShadowTracker full exit dry-run', () => {
  it('drives paper exit FSM and persists net PnL + primary veto reason', () => {
    const db = openDb({ path: ':memory:', memory: true });
    const repos = new Repositories(db);
    let now = 1_000_000;
    const sizeSol = 0.25;
    const tracker = new ShadowTracker(stubRpc, repos, {
      maxConcurrent: 5,
      sizeSol,
      windowMs: 30 * 60_000,
      exits: CFG.exits,
      fees: CFG.fees,
      now: () => now,
    });

    tracker.track({
      mint: 'vetoPump',
      verdict: 'veto',
      primaryVetoCode: 'H7',
      vetoCodes: ['H7', 'LOW_SCORE'],
      baselinePrice: 1,
      poolRef: poolRef('vetoPump'),
    });
    expect(tracker.size).toBe(1);

    // Pump to TP1 (+50% default) — partial sell, then drop to moved stop for remainder.
    now += 5_000;
    tracker.injectTick('vetoPump', 1.5, now);
    // Still open after partial TP1
    expect(tracker.size).toBe(1);

    // After TP1, stop moves to +tp1MoveStopToPct (default 20%) → 1.2; drop through it.
    now += 5_000;
    tracker.injectTick('vetoPump', 1.15, now);

    // Closed and persisted
    expect(tracker.size).toBe(0);

    const row = db
      .prepare(
        `SELECT primary_veto_code, veto_codes_json, net_pnl_sol, gross_pnl_sol, fees_sol,
                exit_reason, hold_ms, peak_mfe_pct, hit_50, size_sol
         FROM shadow_outcomes WHERE mint = 'vetoPump'`,
      )
      .get() as {
      primary_veto_code: string;
      veto_codes_json: string;
      net_pnl_sol: number;
      gross_pnl_sol: number;
      fees_sol: number;
      exit_reason: string;
      hold_ms: number;
      peak_mfe_pct: number;
      hit_50: number;
      size_sol: number;
    };

    expect(row).toBeTruthy();
    expect(row.primary_veto_code).toBe('H7');
    expect(JSON.parse(row.veto_codes_json)).toEqual(['H7', 'LOW_SCORE']);
    expect(row.size_sol).toBe(sizeSol);
    expect(row.peak_mfe_pct).toBeGreaterThanOrEqual(50);
    expect(row.hit_50).toBe(1);
    expect(row.exit_reason).toBeTruthy();
    expect(row.hold_ms).toBeGreaterThan(0);
    // Fee drag applied: net = gross - fees, fees match shipped estimate
    expect(row.fees_sol).toBeGreaterThan(0);
    expect(row.net_pnl_sol).toBeCloseTo(row.gross_pnl_sol - row.fees_sol, 8);
    // Gross PnL positive from TP1 partial at +50%
    expect(row.gross_pnl_sol).toBeGreaterThan(0);

    tracker.stop();
    db.close();
  });

  it('force-closes at window end with TIME_STOP and records exit metrics', () => {
    const db = openDb({ path: ':memory:', memory: true });
    const repos = new Repositories(db);
    let now = 0;
    const tracker = new ShadowTracker(stubRpc, repos, {
      windowMs: 10_000,
      sizeSol: 0.1,
      exits: { ...CFG.exits, timeStopMinutes: 60 }, // time stop later than window
      fees: CFG.fees,
      now: () => now,
    });

    tracker.track({
      mint: 'flat',
      verdict: 'veto',
      primaryVetoCode: 'H5',
      baselinePrice: 1,
      poolRef: poolRef('flat'),
    });

    now = 5_000;
    tracker.injectTick('flat', 1.02, now);
    expect(tracker.size).toBe(1);

    now = 10_000;
    tracker.injectTick('flat', 1.01, now);
    expect(tracker.size).toBe(0);

    const row = db
      .prepare(`SELECT exit_reason, net_pnl_sol, primary_veto_code FROM shadow_outcomes WHERE mint = 'flat'`)
      .get() as { exit_reason: string; net_pnl_sol: number; primary_veto_code: string };

    expect(row.primary_veto_code).toBe('H5');
    expect(row.exit_reason).toBe('TIME_STOP');
    // Flat-ish path minus fee drag → negative net
    expect(row.net_pnl_sol).toBeLessThan(0);

    tracker.stop();
    db.close();
  });

  it('never imports or calls broadcaster/sender on the veto path (structural)', () => {
    // Source-level: shadow module must not reference send/broadcast surfaces.
    const shadowSrc = readFileSync(resolve('src/guardrails/shadow.ts'), 'utf8');
    expect(shadowSrc).not.toMatch(/broadcaster|Executor|buyAndConfirm|\.sell\(|\.buy\(/);
    expect(shadowSrc).toMatch(/PaperPosition/);
    expect(shadowSrc).toMatch(/estimatePaperFees/);
    // Pipeline routes vetoes to shadow.track, accepts to openPosition only.
    const pipelineSrc = readFileSync(resolve('src/guardrails/pipeline.ts'), 'utf8');
    expect(pipelineSrc).toMatch(/shadowTrackVeto/);
    expect(pipelineSrc).toMatch(/this\.shadow\.track/);
    // openPosition is only in the accept branch
    const vetoBranch = pipelineSrc.slice(
      pipelineSrc.indexOf("verdict.verdict === 'veto'"),
      pipelineSrc.indexOf('} else {', pipelineSrc.indexOf("verdict.verdict === 'veto'")),
    );
    expect(vetoBranch).toMatch(/shadowTrackVeto/);
    expect(vetoBranch).not.toMatch(/requestOpen|openPosition/);
  });
});

describe('getVetoDryRunComparison', () => {
  it('separates live accepted PnL from veto dry-run aggregates by reason', () => {
    const db = openDb({ path: ':memory:', memory: true });
    const repos = new Repositories(db);

    // Live accepted closed positions
    db.prepare(
      `INSERT INTO positions (mint, entry_price, size_sol, state, pnl_sol, net_pnl_sol, fees_sol,
        mfe_pct, mae_pct, hold_ms, mode, opened_at, closed_at)
       VALUES ('live1', 1, 0.03, 'CLOSED', 0.01, 0.01, 0.001, 40, -5, 120000, 'live',
               datetime('now'), datetime('now'))`,
    ).run();
    db.prepare(
      `INSERT INTO positions (mint, entry_price, size_sol, state, pnl_sol, net_pnl_sol, fees_sol,
        mfe_pct, mae_pct, hold_ms, mode, opened_at, closed_at)
       VALUES ('live2', 1, 0.03, 'CLOSED', -0.005, -0.005, 0.001, 10, -15, 90000, 'live',
               datetime('now'), datetime('now'))`,
    ).run();

    // Veto dry-runs (shadow_outcomes — never positions)
    repos.recordShadowOutcome({
      mint: 'v1',
      verdict: 'veto',
      primaryVetoCode: 'H7',
      baselinePrice: 1,
      peakPrice: 1.6,
      troughPrice: 0.9,
      peakMfePct: 60,
      maxMaePct: -10,
      hit25: true,
      hit50: true,
      samples: 10,
      trackedMs: 60_000,
      netPnlSol: 0.02,
      holdMs: 50_000,
      exitReason: 'TAKE_PROFIT_1',
    });
    repos.recordShadowOutcome({
      mint: 'v2',
      verdict: 'veto',
      primaryVetoCode: 'H7',
      baselinePrice: 1,
      peakPrice: 1.1,
      troughPrice: 0.8,
      peakMfePct: 10,
      maxMaePct: -20,
      hit25: false,
      hit50: false,
      samples: 8,
      trackedMs: 40_000,
      netPnlSol: -0.015,
      holdMs: 40_000,
      exitReason: 'STOP_LOSS',
    });
    repos.recordShadowOutcome({
      mint: 'v3',
      verdict: 'veto',
      primaryVetoCode: 'LOW_SCORE',
      baselinePrice: 1,
      peakPrice: 2,
      troughPrice: 1,
      peakMfePct: 100,
      maxMaePct: 0,
      hit25: true,
      hit50: true,
      samples: 12,
      trackedMs: 80_000,
      netPnlSol: 0.05,
      holdMs: 70_000,
      exitReason: 'TAKE_PROFIT_2',
    });

    const cmp = getVetoDryRunComparison(db, { range: 'all', mode: 'live' });

    expect(cmp.liveAccepted.n).toBe(2);
    expect(cmp.liveAccepted.netPnlSol).toBeCloseTo(0.005);
    expect(cmp.vetoDryRunTotal).toBe(3);

    const byReason = Object.fromEntries(cmp.byVetoReason.map((r) => [r.primaryVetoCode, r]));
    expect(byReason['H7']!.n).toBe(2);
    expect(byReason['H7']!.netPnlSol).toBeCloseTo(0.005);
    expect(byReason['H7']!.winRatePct).toBeCloseTo(50);
    expect(byReason['LOW_SCORE']!.n).toBe(1);
    expect(byReason['LOW_SCORE']!.hit50Pct).toBe(100);

    // Live net PnL bucket ≠ veto dry-run total (different tables / series)
    const vetoNet = cmp.byVetoReason.reduce((s, r) => s + r.netPnlSol, 0);
    expect(vetoNet).toBeCloseTo(0.055);
    expect(cmp.liveAccepted.netPnlSol).not.toBeCloseTo(vetoNet);

    // Shadow outcomes never appear as closed positions
    const posCount = (db.prepare(`SELECT COUNT(*) AS n FROM positions WHERE mint LIKE 'v%'`).get() as { n: number }).n;
    expect(posCount).toBe(0);

    db.close();
  });
});

describe('estimatePaperFees (shared with shadow dry-run)', () => {
  it('matches the fee-drag formula used for non-live accounting', () => {
    const fees = { swapFeePct: 0.25, estPriorityTipSolPerTx: 0.001 };
    // 1 entry + 2 exit fills
    const f = estimatePaperFees(0.25, 2, fees);
    // tips: 3 * 0.001 = 0.003; swap: 0.0025 * 0.25 * 2 = 0.00125
    expect(f).toBeCloseTo(0.003 + 0.00125);
  });
});
