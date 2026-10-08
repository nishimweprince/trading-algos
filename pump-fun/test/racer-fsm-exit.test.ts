import { describe, expect, it } from 'vitest';
import { ConfigSchema } from '../src/config/schema.ts';
import { openDb } from '../src/persistence/db.ts';
import { Repositories } from '../src/persistence/repositories.ts';
import { getRacerSummary } from '../src/dashboard/queries.ts';
import { simulateFsmExit, type CapturedPath, type PathState } from '../src/racer/pathExit.ts';
import type { PoolMark } from '../src/racer/economics.ts';

const T0 = 1_000_000;
const Q_DEEP = 1e12; // impact ≈ 0: fills ≈ mids, expectations hand-computable
const COSTS = { buyCostSol: 0.001, sellCostSol: 0.0005, failFeeSol: 0.00001 };
const ENTRY: PoolMark = { price: 1e-7, quoteSol: Q_DEEP };
const cfg = ConfigSchema.parse({
  exits: { tp1Pct: 30, tp1SellFraction: 0.75, tp1MoveStopToPct: 5, trailingArmPct: 25, trailingGapPct: 15, hardStopPct: 20, timeStopMinutes: 15 },
}).exits;

const st = (sec: number, price: number, quoteSol = Q_DEEP): PathState => ({
  atMs: T0 + sec * 1000, slot: 100 + sec, signature: `s${sec}`, mark: { price, quoteSol },
});
const gone = (sec: number): PathState => ({ atMs: T0 + sec * 1000, slot: 100 + sec, signature: `g${sec}`, mark: 'gone' });
const path = (states: PathState[], gapsMs: number[] = []): CapturedPath => ({ quality: 'verified', states, gapsMs });

describe('simulateFsmExit', () => {
  it('runs TP1 partial → stop ratchet → trailing exit with live-identical transitions', () => {
    const r = simulateFsmExit(ENTRY, path([st(10, 1.1e-7), st(20, 1.35e-7), st(30, 1.3e-7), st(40, 1.4e-7), st(50, 1.15e-7), st(60, 1.14e-7)]),
      T0, 0.1, COSTS, cfg, { maxHoldMs: 300_000 });
    expect(r.reason).toBe('trail');
    expect(r.legs.map((l) => [l.trigger, l.fraction, l.fill])).toEqual([
      ['TAKE_PROFIT_1', 0.75, 'next-slot'],
      ['TRAILING_STOP', 0.25, 'next-slot'],
    ]);
    expect(r.legs[0]!.exitAtMs).toBe(T0 + 30_000);
    expect(r.legs[1]!.exitAtMs).toBe(T0 + 60_000);
    // Hand-computed at 125 bps/leg (mcap 100–140 SOL): buy 0.09875 in →
    // TP1 740,625 tok × 1.3e-7 = 0.09628125 × 0.9875 − 0.0005 = 0.09457773;
    // trail 246,875 tok × 1.14e-7 = 0.02814375 × 0.9875 − 0.0005 = 0.02729195;
    // (0.12186969 − 0.001 − 0.1) / 0.1 = +20.87%.
    expect(r.pnlPct).toBeCloseTo(20.87, 2);
    expect(r.legs.every((l) => !l.emergency)).toBe(true);
  });
  it('zeroes the remainder on a mid-hold rug, keeping filled legs', () => {
    const r = simulateFsmExit(ENTRY, path([st(10, 1.35e-7), st(20, 1.3e-7), gone(30)]), T0, 0.1, COSTS, cfg, { maxHoldMs: 300_000 });
    expect(r.reason).toBe('rug');
    expect(r.legs.map((l) => l.trigger)).toEqual(['TAKE_PROFIT_1']);
    // TP1 leg only (0.09457773 above), remainder lost, buy paid:
    // (0.09457773 − 0.001 − 0.1) / 0.1 = −6.42%.
    expect(r.pnlPct).toBeCloseTo(-6.42, 2);
  });
  it('stays unpriced when a fill would cross a stream gap', () => {
    const r = simulateFsmExit(ENTRY, path([st(10, 1.35e-7), st(20, 1.3e-7)], [T0 + 15_000]), T0, 0.1, COSTS, cfg, { maxHoldMs: 300_000 });
    expect(r.reason).toBe('unpriced-exit');
    expect(r.pnlPct).toBeNull();
  });
  it('escalates past the worst ordinary tier inside the emergency bound', () => {
    // Stop triggers at 0.79 (−21%); the fill pool holds 0.05 SOL against
    // ~0.078 notional: ~61% impact → emergency, still fillable under 90.
    const r = simulateFsmExit(ENTRY, path([st(10, 0.79e-7), st(20, 0.79e-7, 0.05), st(30, 0.79e-7, 100)]),
      T0, 0.1, COSTS, cfg, { maxHoldMs: 300_000, maxExitSlippagePct: 25, emergencySlippagePct: 90 });
    expect(r.reason).toBe('stop');
    expect(r.legs).toHaveLength(1);
    expect(r.legs[0]).toMatchObject({ trigger: 'STOP_LOSS', emergency: true, attempts: 1 });
  });
  it('skips unfillable candidates (never sent, never paid) and fills deeper', () => {
    // First candidate: ~0.078 notional into 0.001 SOL ≈ 99% impact → skipped.
    const r = simulateFsmExit(ENTRY, path([st(10, 0.79e-7), st(20, 0.79e-7, 0.001), st(30, 0.79e-7, 100)]),
      T0, 0.1, COSTS, cfg, { maxHoldMs: 300_000, maxExitSlippagePct: 25, emergencySlippagePct: 90 });
    expect(r.legs).toHaveLength(1);
    expect(r.legs[0]).toMatchObject({ emergency: false, attempts: 2, exitAtMs: T0 + 30_000 });
    expect(r.legs[0]!.costSol).toBe(0.0005); // the skipped send cost nothing
  });
  it('exits flat holds at the deadline and still pools unchanged', () => {
    const r = simulateFsmExit(ENTRY, path([st(10, 1.01e-7), st(20, 1.02e-7), st(30, 1.01e-7)]), T0, 0.1, COSTS, cfg, { maxHoldMs: 25_000 });
    expect(r.reason).toBe('time');
    expect(r.legs).toHaveLength(1);
    expect(r.legs[0]).toMatchObject({ trigger: 'TIME_STOP', fill: 'unchanged' });
    expect(r.fill).toBe('unchanged');
  });
  it('exits at the entry mark when the pool never traded (continuous only)', () => {
    const still = simulateFsmExit(ENTRY, path([]), T0, 0.1, COSTS, cfg, { maxHoldMs: 300_000 });
    expect(still.reason).toBe('time');
    expect(still.legs).toHaveLength(1);
    expect(still.pnlPct!).toBeLessThan(0); // fees + costs on a round trip to nowhere
    const gapped = simulateFsmExit(ENTRY, path([], [T0 + 5_000]), T0, 0.1, COSTS, cfg, { maxHoldMs: 300_000 });
    expect(gapped.reason).toBe('unpriced-exit');
  });
  it('replays identical failures for identical seeds', () => {
    const states = [st(10, 1.35e-7), st(20, 1.3e-7), st(30, 1.25e-7), st(40, 1.2e-7), st(50, 1.1e-7), st(60, 1.05e-7)];
    const run = () => simulateFsmExit(ENTRY, path(states), T0, 0.1, COSTS, cfg, { maxHoldMs: 300_000, seed: 'M1', exitP: 0.5 });
    expect(run()).toEqual(run());
    const run2 = () => simulateFsmExit(ENTRY, path(states), T0, 0.1, COSTS, cfg, { maxHoldMs: 300_000, seed: 'M2', exitP: 0.5 });
    expect(run2()).toEqual(run2());
  });
  it('with exitP 0 every send fails and the sim cannot complete', () => {
    const r = simulateFsmExit(ENTRY, path([st(10, 1.35e-7), st(20, 1.3e-7)]), T0, 0.1, COSTS, cfg, { maxHoldMs: 300_000, seed: 'M', exitP: 0 });
    expect(r.reason).toBe('unpriced-exit');
  });
});

describe('path capture accounting', () => {
  it('breaks recorded paths down by quality and gaps', () => {
    const db = openDb({ path: ':memory:', memory: true });
    const repos = new Repositories(db);
    const track = { poolAddress: 'P', entrySlot: 1, sizeSol: 0.1, openPrice: 1e-7, openQuoteSol: 85, reachPrice: 1e-7, reachQuoteSol: 85,
      horizonsJson: '[]', entryModelVersion: 2, entryQuality: 'verified' };
    for (const mint of ['A', 'B', 'C']) repos.recordRacerPaperTrack({ ...track, mint });
    repos.recordRacerPath('A', JSON.stringify({ version: 2, quality: 'verified', gapsMs: [] }), '{}');
    repos.recordRacerPath('B', JSON.stringify({ version: 2, quality: 'verified', gapsMs: [123] }), '{}');
    repos.recordRacerPath('C', JSON.stringify({ version: 2, quality: 'missing-anchor', gapsMs: [] }), '{}');
    expect(getRacerSummary(db).paper.pathCapture).toEqual({ recorded: 3, byQuality: { verified: 2, 'missing-anchor': 1 }, gapHolds: 1 });
  });
});
