import { describe, expect, it } from 'vitest';
import {
  blendFillForLanding,
  scoreDistributedLegs,
  tallyLandingOffsets,
  type LandingWeights,
} from '../src/racer/landing.ts';
import { adjustLegForFill } from '../src/racer/fills.ts';
import { roundTripParts } from '../src/racer/economics.ts';
import { PaperTracker, type PaperTrack } from '../src/racer/paper.ts';
import { openDb } from '../src/persistence/db.ts';
import { buildRacerPaperCsv } from '../src/dashboard/queries.ts';

const W: LandingWeights = { w1: 0.5, w2: 0.3, w3: 0.1, tail: 0.1, n: 100 };

describe('tallyLandingOffsets', () => {
  it('floors early-ready offsets at +1 and spills past +3 into the tail', () => {
    expect(tallyLandingOffsets([0, 1, 1, 2, 3, 4, 5])).toEqual({ w1: 3 / 7, w2: 1 / 7, w3: 1 / 7, tail: 2 / 7, n: 7 });
  });

  it('skips unmappable samples and fails open on none', () => {
    expect(tallyLandingOffsets([null, 1, null])?.n).toBe(1);
    expect(tallyLandingOffsets([])).toBeNull();
    expect(tallyLandingOffsets([null, null])).toBeNull();
  });
});

describe('blendFillForLanding', () => {
  it('blends window p and charges the tail at the offset-3 attempt rate', () => {
    const b = blendFillForLanding(
      {
        slot1: { p: 0.6, costSol: 0.001 },
        slot2: { p: 0.5, costSol: 0.001 },
        slot3: { p: 0.4, costSol: 0.002, exitFailCostSol: 0.0005 },
      },
      W,
    )!;
    expect(b.p).toBeCloseTo(0.5 * 0.6 + 0.3 * 0.5 + 0.1 * 0.4, 12);
    expect(b.costSol).toBeCloseTo(0.5 * 0.001 + 0.3 * 0.001 + 0.1 * 0.002 + 0.1 * 0.002, 12);
    expect(b.exitFailCostSol).toBe(0.0005);
  });

  it('excludes unpriced offsets and is null when nothing is priced', () => {
    const b = blendFillForLanding({ slot1: { p: 0.6, costSol: 0.001 }, slot2: null, slot3: null }, W)!;
    expect(b.p).toBeCloseTo(0.5 * 0.6, 12);
    expect(blendFillForLanding({ slot1: null, slot2: null, slot3: null }, W)).toBeNull();
  });
});

describe('scoreDistributedLegs', () => {
  const legs = {
    slot1: 10, slot1_adj: 4,
    slot2: 20, slot2_adj: 8,
    slot3: 30, slot3_adj: 10,
  };
  const fills = {
    slot1: { p: 0.6, costSol: 0.001 },
    slot2: { p: 0.5, costSol: 0.001 },
    slot3: { p: 0.4, costSol: 0.002, exitFailCostSol: 0.0005 },
  };

  it('prices the window expectancy and charges the tail without a fill', () => {
    const { value, adj } = scoreDistributedLegs({ legs, gateReasons: undefined, fills, sizeSol: 0.1, weights: W, suffix: '' });
    expect(value).toBeCloseTo((0.5 * 10 + 0.3 * 20 + 0.1 * 30) / 0.9, 12);
    expect(adj).toBeCloseTo(0.5 * 4 + 0.3 * 8 + 0.1 * 10 + 0.1 * -2.5, 12);
  });

  it('charges gate-impossible offsets as attempts without a fill', () => {
    const gapped = { ...legs, slot2: null, slot2_adj: null };
    const { value, adj } = scoreDistributedLegs({
      legs: gapped, gateReasons: { slot2: 'entry-impact' }, fills, sizeSol: 0.1, weights: W, suffix: '',
    });
    expect(value).toBeCloseTo((0.5 * 10 + 0.1 * 30) / 0.6, 12);
    // slot2 attempts burn at the slot2 rate (0.001/0.1 = 1%), tail at slot3 (2.5%).
    expect(adj).toBeCloseTo(0.5 * 4 + 0.3 * -1 + 0.1 * 10 + 0.1 * -2.5, 12);
  });

  it('fails the expectancy open when mass is missing, but still prices the conditional base', () => {
    const gapped = { ...legs, slot2: null, slot2_adj: null };
    const { value, adj } = scoreDistributedLegs({
      legs: gapped, gateReasons: undefined, fills, sizeSol: 0.1, weights: W, suffix: '',
    });
    expect(value).toBeCloseTo((0.5 * 10 + 0.1 * 30) / 0.6, 12);
    expect(adj).toBeNull();
  });

  it('is null without coverage and reads the mid window for feasiblemid', () => {
    expect(scoreDistributedLegs({ legs: {}, gateReasons: undefined, fills, sizeSol: 0.1, weights: W, suffix: '' }))
      .toEqual({ value: null, adj: null });
    const mid = scoreDistributedLegs({
      legs: { slot1mid: 10, slot1mid_adj: 4, slot2mid: 20, slot2mid_adj: 8, slot3mid: 30, slot3mid_adj: 10 },
      gateReasons: undefined,
      fills: { slot1mid: fills.slot1, slot2mid: fills.slot2, slot3mid: fills.slot3 },
      sizeSol: 0.1, weights: W, suffix: 'mid',
    });
    expect(mid.value).toBeCloseTo((0.5 * 10 + 0.3 * 20 + 0.1 * 30) / 0.9, 12);
  });
});

describe('distributed feasible legs in the tracker', () => {
  it('prices feasible/feasiblemid as expectancies over the slot legs', async () => {
    const sunk: PaperTrack[] = [];
    const paper = new PaperTracker(
      {
        readMark: async () => ({ price: 1.1, quoteSol: 10 }),
        sink: (t) => sunk.push(t),
        schedule: (_ms, fn) => void fn(),
      },
      { horizonsSec: [60], sizeSol: 0.1, liveCostSol: 0, exitLandMs: 0 },
    );
    const entry = (price: number) => ({ price, quoteSol: 10 });
    const fill = (p: number, costSol: number) => ({ p, costSol });
    paper.track('M', {
      poolAddress: 'P', slot: 1, open: null, reach: entry(1),
      delayed: {
        slot1: entry(1), slot2: entry(1.05), slot3: entry(0.95),
        slot1mid: entry(1.01), slot2mid: entry(1.06), slot3mid: entry(0.96),
        feasible: null, feasiblemid: null,
      },
      fill: {
        slot1: fill(0.6, 0.001), slot2: fill(0.5, 0.001), slot3: fill(0.4, 0.002),
        slot1mid: fill(0.6, 0.001), slot2mid: fill(0.5, 0.001), slot3mid: fill(0.4, 0.002),
      },
      landingWeights: W,
    });
    for (let i = 0; i < 100 && paper.pendingCount > 0; i++) await new Promise((r) => setImmediate(r));
    expect(paper.pendingCount).toBe(0);
    const h = sunk[sunk.length - 1]!.horizons[0]!;
    const exit = { price: 1.1, quoteSol: 10 };
    const leg = (price: number) => roundTripParts(entry(price), exit, 0.1)!.pct;
    const l1 = leg(1);
    const l2 = leg(1.05);
    const l3 = leg(0.95);
    expect(h.pnl.feasible).toBeCloseTo((0.5 * l1 + 0.3 * l2 + 0.1 * l3) / 0.9, 9);
    expect(h.pnl.feasible_adj).toBeCloseTo(
      0.5 * adjustLegForFill(l1, { p: 0.6, costSol: 0.001 }, 0.1)! +
        0.3 * adjustLegForFill(l2, { p: 0.5, costSol: 0.001 }, 0.1)! +
        0.1 * adjustLegForFill(l3, { p: 0.4, costSol: 0.002 }, 0.1)! +
        0.1 * -((0.002 / 0.1) * 100),
      9,
    );
    expect(h.pnl.feasiblemid).not.toBeNull();
  });
});

describe('paper CSV landing weights', () => {
  it('carries the distribution JSON on each horizon row', () => {
    const db = openDb({ path: ':memory:', memory: true });
    db.prepare(`INSERT INTO racer_paper_tracks
      (mint, size_sol, reach_price, reach_quote_sol, horizons_json, entries_json, created_at, armed,
       entry_model_version, entry_quality, landing_weights_json)
      VALUES ('A', 0.1, 1, 10, '[{"sec":60,"pnl":{"slot1mid":5}}]', '{}', '2026-01-01 00:00:00', 1,
       2, 'verified', '{"w1":0.5,"w2":0.3,"w3":0.1,"tail":0.1,"n":100}')`).run();
    const [header, row] = buildRacerPaperCsv(db, {}).trim().split('\n');
    expect(header!.split(',')).toContain('landing_weights_json');
    expect(row!).toContain('w1');
  });
});
