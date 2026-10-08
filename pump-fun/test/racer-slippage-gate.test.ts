import { describe, expect, it } from 'vitest';
import { openDb } from '../src/persistence/db.ts';
import { Repositories } from '../src/persistence/repositories.ts';
import { buildRacerPaperCsv } from '../src/dashboard/queries.ts';
import { paperRoundTripPct, roundTripGate, roundTripParts, type PoolMark } from '../src/racer/economics.ts';
import { PaperTracker, type PaperTrack } from '../src/racer/paper.ts';

const mark = (price: number, quoteSol: number, realQuoteSol?: number): PoolMark =>
  realQuoteSol === undefined ? { price, quoteSol } : { price, quoteSol, realQuoteSol };

describe('roundTripParts', () => {
  it('is bit-identical to the historic round-trip value', () => {
    const cases: Array<[PoolMark, PoolMark, number]> = [
      [mark(1e-7, 85), mark(1.2e-7, 90), 0.1],
      [mark(4.1e-7, 84.99), mark(0.0014, 5025), 0.1],
      [mark(1e-7, 0.5), mark(1e-7, 0.5), 0.1],
      [mark(1e-7, 85, 10), mark(2e-7, 3, 0.05), 0.1], // liquidity-capped exit
      [mark(0, 85), mark(1e-7, 85), 0.1], // unusable
    ];
    for (const [entry, exit, size] of cases) {
      expect(roundTripParts(entry, exit, size)?.pct ?? null).toBe(paperRoundTripPct(entry, exit, size));
    }
  });
  it('exposes pre-fee impacts on both legs', () => {
    const p = roundTripParts(mark(1e-7, 1), mark(1e-7, 1), 0.1)!;
    expect(p.entryImpactPct).toBeGreaterThan(9); // 0.1/1, net of fee
    expect(p.entryImpactPct).toBeLessThan(10);
    expect(p.exitImpactPct).toBeGreaterThan(0);
  });
});

describe('roundTripGate', () => {
  it('fires above the cap, passes at it, disables on undefined', () => {
    expect(roundTripGate({ pct: 0, entryImpactPct: 8, exitImpactPct: 0 }, 8, 25)).toBeNull();
    expect(roundTripGate({ pct: 0, entryImpactPct: 8.0001, exitImpactPct: 0 }, 8, 25)).toBe('entry-slippage');
    expect(roundTripGate({ pct: 0, entryImpactPct: 0, exitImpactPct: 25 }, 8, 25)).toBeNull();
    expect(roundTripGate({ pct: 0, entryImpactPct: 0, exitImpactPct: 25.0001 }, 8, 25)).toBe('exit-slippage');
    expect(roundTripGate({ pct: 0, entryImpactPct: 99, exitImpactPct: 99 })).toBeNull();
  });
  it('treats uncomputable marks as unpriced, not gated', () => {
    expect(roundTripGate(null, 8, 25)).toBeNull();
  });
});

describe('paper slippage gate', () => {
  const drive = async (entry: PoolMark, exit: PoolMark, opts: { maxEntrySlippagePct?: number; maxExitSlippagePct?: number }) => {
    const tracks: PaperTrack[] = [];
    const p = new PaperTracker(
      { readMark: async () => exit, sink: (t) => void tracks.push(JSON.parse(JSON.stringify(t))), schedule: (_ms, fn) => fn() },
      { horizonsSec: [30], sizeSol: 0.1, ...opts },
    );
    p.track('M', { poolAddress: 'P', slot: 1, open: null, reach: entry, delayed: { slot1: entry } });
    await new Promise((r) => setTimeout(r, 0));
    return tracks.at(-1)!;
  };
  it('nulls thin-entry legs with entry-slippage', async () => {
    const t = await drive(mark(1e-7, 0.5), mark(1e-7, 100), { maxEntrySlippagePct: 8, maxExitSlippagePct: 25 });
    const h = t.horizons[0]!;
    expect(h.pnlReachPct).toBeNull();
    expect(h.pnl.slot1).toBeNull();
    expect(h.gateReasons).toEqual({ reach: 'entry-slippage', slot1: 'entry-slippage' });
  });
  it('nulls thin-exit legs with exit-slippage', async () => {
    // Deep entry, 0.1 SOL of notional into 0.1 SOL of exit depth: 50% exit impact.
    const t = await drive(mark(1e-7, 1e9), mark(1e-7, 0.1), { maxEntrySlippagePct: 8, maxExitSlippagePct: 25 });
    const h = t.horizons[0]!;
    expect(h.pnlReachPct).toBeNull();
    expect(h.pnl.slot1).toBeNull();
    expect(h.gateReasons).toEqual({ reach: 'exit-slippage', slot1: 'exit-slippage' });
  });
  it('scores deep pools and stays off without caps', async () => {
    const deep = await drive(mark(1e-7, 100), mark(1e-7, 100), { maxEntrySlippagePct: 8, maxExitSlippagePct: 25 });
    expect(deep.horizons[0]!.pnlReachPct).not.toBeNull();
    expect(deep.horizons[0]!.pnl.slot1).not.toBeNull();
    expect(deep.horizons[0]!.gateReasons).toBeUndefined();
    const uncapped = await drive(mark(1e-7, 0.5), mark(1e-7, 100), {});
    expect(uncapped.horizons[0]!.pnlReachPct).not.toBeNull();
    expect(uncapped.horizons[0]!.gateReasons).toBeUndefined();
  });
});

describe('gate reasons in exports', () => {
  it('carries leg_gate_json in the CSV', () => {
    const db = openDb({ path: ':memory:', memory: true });
    new Repositories(db).recordRacerPaperTrack({
      mint: 'G', poolAddress: 'P', entrySlot: 1, sizeSol: 0.1, openPrice: null, openQuoteSol: null,
      reachPrice: 1e-7, reachQuoteSol: 0.5,
      horizonsJson: JSON.stringify([{ sec: 30, price: 1e-7, quoteSol: 100, pnlOpenPct: null, pnlReachPct: null,
        pnl: { reach: null }, gateReasons: { reach: 'entry-slippage' } }]),
      entryModelVersion: 2, entryQuality: 'verified',
    });
    const csv = buildRacerPaperCsv(db);
    expect(csv).toContain('leg_gate_json');
    expect(csv).toContain('entry-slippage');
  });
});
