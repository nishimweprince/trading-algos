import { describe, expect, it } from 'vitest';
import { openDb } from '../src/persistence/db.ts';
import { Repositories } from '../src/persistence/repositories.ts';
import { buildRacerPaperCsv, getRacerSummary, listRacerPaper } from '../src/dashboard/queries.ts';
import { checkOpenMark, checkOpenSlotDrift, isPlausibleStoredOpen, markSlotDisagree, migrateSlotDisagree } from '../src/racer/markChecks.ts';
import type { PoolMark } from '../src/racer/economics.ts';

const mark = (price: number): PoolMark => ({ price, quoteSol: 30 });

describe('checkOpenMark', () => {
  const reach = mark(0.0014);
  it('passes an unknown open through as unknown, not rejected', () => {
    expect(checkOpenMark(null, reach, 0n, 100n)).toEqual({ open: null, reason: null });
  });
  it('passes an open equal to the reach without a drain check', () => {
    // No bundled buy: the open IS the reach, even if the caller gained tokens elsewhere.
    expect(checkOpenMark(mark(0.0014), reach, 10_000n, 100n)).toEqual({ open: mark(0.0014), reason: null });
  });
  it('rejects non-positive and above-reach opens', () => {
    expect(checkOpenMark(mark(0), reach, 1n, 100n).reason).toBe('non-positive');
    expect(checkOpenMark(mark(0.002), reach, 1n, 100n).reason).toBe('open-above-reach');
  });
  it('rejects a drain above the bound, passes at the bound', () => {
    // drain 0.75 exactly: bundled 300 of pre-buy 400.
    expect(checkOpenMark(mark(0.001), reach, 300n, 100n).reason).toBeNull();
    expect(checkOpenMark(mark(0.001), reach, 301n, 100n).reason).toBe('implausible-drain');
    // The live 2026-10-05 shape: ratio² ≈ 1/3500, drain ≈ 98%.
    expect(checkOpenMark(mark(4.1e-7), reach, 5_700n, 100n).reason).toBe('implausible-drain');
  });
  it('skips the drain check when nothing was bundled', () => {
    expect(checkOpenMark(mark(0.001), reach, 0n, 100n)).toEqual({ open: mark(0.001), reason: null });
  });
});

describe('checkOpenSlotDrift', () => {
  it('is null-safe on both sides', () => {
    expect(checkOpenSlotDrift(null, 0.0014)).toBeNull();
    expect(checkOpenSlotDrift(mark(0.001), null)).toBeNull();
    expect(checkOpenSlotDrift(mark(0.001), 0)).toBeNull();
  });
  it('rejects 1000x+ derivation errors either way, passes real moves', () => {
    expect(checkOpenSlotDrift(mark(4.1e-7), 0.0014)).toBe('slot-drift');
    expect(checkOpenSlotDrift(mark(0.0014), 4.1e-7)).toBe('slot-drift');
    expect(checkOpenSlotDrift(mark(0.001), 0.002)).toBeNull();
    expect(checkOpenSlotDrift(mark(20), 1)).toBeNull(); // 20x, at the bound
    expect(checkOpenSlotDrift(mark(20.01), 1)).toBe('slot-drift');
    expect(checkOpenSlotDrift(mark(1), 20)).toBeNull(); // 1/20, at the bound
    expect(checkOpenSlotDrift(mark(1), 20.01)).toBe('slot-drift');
  });
});

describe('isPlausibleStoredOpen', () => {
  it('mirrors the drain bound through the mark ratio (floor = (1-0.75)² = 1/16)', () => {
    expect(isPlausibleStoredOpen(1, 16)).toBe(true);
    expect(isPlausibleStoredOpen(0.999, 16)).toBe(false);
    expect(isPlausibleStoredOpen(4.1e-7, 0.0014)).toBe(false);
    expect(isPlausibleStoredOpen(0.0012, 0.0014)).toBe(true);
    expect(isPlausibleStoredOpen(null, 0.0014)).toBe(false);
    expect(isPlausibleStoredOpen(0.002, 0.0014)).toBe(false);
  });
});

describe('markSlotDisagree / migrateSlotDisagree', () => {
  it('fires only on positive >20x evidence, either direction', () => {
    expect(markSlotDisagree(4.1e-7, 0.0014)).toBe(true);
    expect(markSlotDisagree(0.0014, 4.1e-7)).toBe(true);
    expect(markSlotDisagree(0.0012, 0.0014)).toBe(false);
    expect(migrateSlotDisagree(4.1e-7, 4.1e-7, 0.0014)).toBe(true);
    expect(migrateSlotDisagree(0.0012, 0.0014, 0.0014)).toBe(false);
  });
  it('fails open when either side is missing', () => {
    expect(markSlotDisagree(null, 0.0014)).toBe(false);
    expect(markSlotDisagree(4.1e-7, null)).toBe(false);
    expect(migrateSlotDisagree(4.1e-7, 4.1e-7, null)).toBe(false);
  });
});

describe('open-mark honesty in queries', () => {
  const seed = () => {
    const db = openDb({ path: ':memory:', memory: true });
    const repos = new Repositories(db);
    // Honest verified track: stored ok, slot marks corroborate.
    repos.recordRacerPaperTrack({
      mint: 'GOOD', poolAddress: 'P', entrySlot: 1, sizeSol: 0.1, openPrice: 0.0012, openQuoteSol: 30,
      reachPrice: 0.0014, reachQuoteSol: 32,
      horizonsJson: JSON.stringify([{ sec: 30, price: 0.0015, quoteSol: 33, pnlOpenPct: 5, pnlReachPct: 3, pnl: { open: 5, reach: 3 } }]),
      entriesJson: JSON.stringify({ slot1: { price: 0.0013, quoteSol: 31 }, slot1mid: { price: 0.0013, quoteSol: 31 } }),
      entryModelVersion: 2, entryQuality: 'verified',
      openQuality: 'ok', openRejectReason: null,
    });
    // Legacy constant-open shape (Unh4, live 2026-10-05): migrate marks 3500x
    // from the row's own slot marks. No stored flags; verdict is derived.
    repos.recordRacerPaperTrack({
      mint: 'BAD', poolAddress: 'P', entrySlot: 2, sizeSol: 0.1, openPrice: 4.1e-7, openQuoteSol: 85,
      reachPrice: 4.1e-7, reachQuoteSol: 85,
      horizonsJson: JSON.stringify([{ sec: 30, price: 0.0015, quoteSol: 5038, pnlOpenPct: 322852, pnlReachPct: 322852, pnl: { open: 322852, reach: 322852 } }]),
      entriesJson: JSON.stringify({ slot1: { price: 0.00144, quoteSol: 5025 }, slot1mid: { price: 0.00144, quoteSol: 5025 } }),
      entryModelVersion: null, entryQuality: null,
    });
    // Legacy track with no slot marks: nothing to corroborate against.
    repos.recordRacerPaperTrack({
      mint: 'NOCORROB', poolAddress: 'P', entrySlot: 3, sizeSol: 0.1, openPrice: 4.1e-7, openQuoteSol: 85,
      reachPrice: 4.1e-7, reachQuoteSol: 85,
      horizonsJson: JSON.stringify([{ sec: 30, price: 0.0015, quoteSol: 5038, pnlOpenPct: 322852, pnlReachPct: 322852, pnl: { open: 322852, reach: 322852 } }]),
      entriesJson: '{}', entryModelVersion: null, entryQuality: null,
    });
    return db;
  };
  it('derives verdicts for flagless rows and exposes stored ones', () => {
    const rows = listRacerPaper(seed(), { all: true });
    const good = rows.find((r) => r.mint === 'GOOD')!;
    const bad = rows.find((r) => r.mint === 'BAD')!;
    const nocorrob = rows.find((r) => r.mint === 'NOCORROB')!;
    expect(good.openQuality).toBe('ok');
    expect(good.openRejectReason).toBeNull();
    expect(bad.openQuality).toBe('rejected');
    expect(bad.openRejectReason).toBe('migrate-slot-disagree');
    expect(nocorrob.openQuality).toBe('unverifiable');
    expect(nocorrob.openRejectReason).toBeNull();
  });
  it('excludes disagreeing migrate legs from the medians', () => {
    // Promote BAD to verified so the aggregation would count it without the guard.
    const db = seed();
    db.prepare(`UPDATE racer_paper_tracks SET entry_model_version = 2, entry_quality = 'verified' WHERE mint = 'BAD'`).run();
    const s = getRacerSummary(db);
    const h30 = s.paper.horizons.find((h) => h.sec === 30)!;
    expect(h30.entries.open).toMatchObject({ n: 1, median: 5 });
    expect(h30.entries.reach).toMatchObject({ n: 1, median: 3 });
  });
  it('carries the quality columns in the CSV', () => {
    const csv = buildRacerPaperCsv(seed());
    const [header, ...lines] = csv.trim().split('\n');
    expect(header).toContain('open_quality,open_reject_reason');
    expect(lines.some((l) => l.includes(',rejected,migrate-slot-disagree'))).toBe(true);
  });
  it('filters the verified cohort', () => {
    const db = seed();
    expect(listRacerPaper(db, { all: true, cohort: 'verified' }).map((r) => r.mint)).toEqual(['GOOD']);
    expect(buildRacerPaperCsv(db, { cohort: 'verified' })).toContain('GOOD');
  });
  it('hides missing-anchor tracks from display lists by default', () => {
    const db = seed();
    const repos = new Repositories(db);
    for (const mint of ['NOANCHOR', 'NULLQ']) {
      repos.recordRacerPaperTrack({
        mint, poolAddress: 'P', entrySlot: 9, sizeSol: 0.1, openPrice: 0.0012, openQuoteSol: 30,
        reachPrice: 0.0014, reachQuoteSol: 32,
        horizonsJson: JSON.stringify([{ sec: 30, price: 0.0015, quoteSol: 33, pnlOpenPct: 5, pnlReachPct: 3 }]),
        entriesJson: '{}', entryModelVersion: 2, entryQuality: 'missing-anchor',
      });
    }
    // The repo coerces null quality to 'legacy-unverified' on write; NULLQ
    // simulates a true v2+NULL row (pre-flag write path), which also displays
    // as missing-anchor per the row mapping.
    db.prepare(`UPDATE racer_paper_tracks SET entry_quality = NULL WHERE mint = 'NULLQ'`).run();
    // Display surfaces (table, CSV, bundle) exclude them: no anchor, no verifiable entry.
    expect(listRacerPaper(db, { all: true }).map((r) => r.mint).sort()).toEqual(['BAD', 'GOOD', 'NOCORROB']);
    expect(buildRacerPaperCsv(db)).not.toContain('NOANCHOR');
    expect(buildRacerPaperCsv(db)).not.toContain('NULLQ');
    // Internal stats opt back in so exclusion counts stay honest.
    expect(listRacerPaper(db, { all: true, includeUnanchored: true }).map((r) => r.mint).sort())
      .toEqual(['BAD', 'GOOD', 'NOANCHOR', 'NOCORROB', 'NULLQ']);
  });
});
