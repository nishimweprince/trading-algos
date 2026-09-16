import { describe, expect, it } from 'vitest';
import { openDb } from '../src/persistence/db.ts';
import { Repositories } from '../src/persistence/repositories.ts';
import { buildRugForensics, renderRugForensicsMarkdown } from '../src/dashboard/rugForensics.ts';

function seed() {
  const db = openDb({ path: ':memory:', memory: true });
  const repos = new Repositories(db);
  const now = Date.now();
  const insertCandidate = db.prepare(
    `INSERT INTO candidates (mint, enrichment_json, verdict, creator_share, top10_share, pool_sol_at_entry, early_flow_net_sol)
     VALUES (?, ?, 'accept', ?, ?, ?, ?)`,
  );
  const mk = (mint: string, pnlPct: number, creator: string, creatorShare: number, top10: number) => {
    insertCandidate.run(mint, JSON.stringify({ pool: { coinCreator: creator } }), creatorShare, top10, 30, 0.2);
    repos.upsertDryRunPosition({
      mint,
      state: 'CLOSED',
      liveStatus: 'entered',
      sizeSol: 0.1,
      entryPrice: 1,
      exitPrice: 1 + pnlPct / 100,
      exitReason: pnlPct < -80 ? 'STOP_LOSS' : 'TIME_STOP',
      openedAt: now - 300_000,
      closedAt: now - 60_000,
      grossPnlSol: (0.1 * pnlPct) / 100,
      feesSol: 0.001,
      netPnlSol: (0.1 * pnlPct) / 100 - 0.001,
      pnlPct,
      mfePct: 4,
      holdMs: 240_000,
      mode: 'live',
    });
  };
  mk('rug1', -99, 'devA', 4.5, 24);
  mk('rug2', -95, 'devA', 4.8, 23);
  mk('rug3', -90, 'devB', 4.9, 22);
  mk('ok1', 5, 'devC', 0.5, 12);
  mk('ok2', 12, 'devD', 1.0, 15);
  mk('ok3', -18, 'devE', 0.8, 14);
  mk('ok4', 30, 'devA', 0.2, 10);
  return db;
}

describe('rug forensics', () => {
  it('splits cohorts, ranks separating features and flags repeat creators', () => {
    const r = buildRugForensics(seed(), { track: 'dry', range: 'all' });
    expect(r.trades).toBe(7);
    expect(r.rugs).toBe(3);
    expect(r.rugRatePct).toBeCloseTo(42.86, 1);
    expect(r.rugMedianHoldS).toBe(240);
    expect(r.rugMints.sort()).toEqual(['rug1', 'rug2', 'rug3']);

    const creatorShare = r.features.find((f) => f.feature === 'creator_share')!;
    expect(creatorShare.rug.p50).toBe(4.8);
    expect(creatorShare.other.p75).toBeLessThan(2);
    expect(creatorShare.rugShareAboveOtherP75).toBe(1); // every rug sits above the clean p75

    const pool = r.features.find((f) => f.feature === 'pool_sol_at_entry')!;
    expect(pool.rug.p50).toBe(pool.other.p50); // no separation → not a lever

    expect(r.repeatCreators).toEqual([{ creator: 'devA', rugs: 2, trades: 3 }]);
  });

  it('renders a markdown summary with the feature table', () => {
    const md = renderRugForensicsMarkdown(buildRugForensics(seed(), { range: 'all' }));
    expect(md).toMatch(/\*\*3 rugs\*\*/);
    expect(md).toMatch(/\| creator_share \|/);
    expect(md).toMatch(/devA \| 2 \| 3/);
  });

  it('honours the rug threshold and the live track', () => {
    const db = seed();
    expect(buildRugForensics(db, { range: 'all', rugPnlPct: -15 }).rugs).toBe(4);
    expect(buildRugForensics(db, { track: 'live', range: 'all' }).trades).toBe(0);
  });
});
