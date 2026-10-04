import { describe, expect, it } from 'vitest';
import { openDb } from '../src/persistence/db.ts';
import { Repositories } from '../src/persistence/repositories.ts';
import { ConfigSchema } from '../src/config/schema.ts';
import { getDashboardSummary, listPositions, trackSource } from '../src/dashboard/queries.ts';

describe('dashboard tabs by bot mode', () => {
  it('a dry-run bot shows its simulated trades on the dry tab, never on live', () => {
    const db = openDb({ path: ':memory:', memory: true });
    const repos = new Repositories(db);
    const now = Date.now();
    repos.upsertPosition(
      { mint: 'SIM', state: 'CLOSED', sizeSol: 0.02, entryPrice: 1, openedAt: now, closedAt: now, pnlSol: 0.004, pnlPct: 20 },
      { mode: 'dry-run', netPnlSol: 0.004, feesSol: 0.0005 },
    );
    repos.upsertPosition({ mint: 'OPENSIM', state: 'OPEN', sizeSol: 0.02, entryPrice: 1, openedAt: now }, { mode: 'dry-run' });
    const config = ConfigSchema.parse({ mode: 'dry-run' });

    const dry = getDashboardSummary(db, config, { track: 'dry' });
    expect(dry.pnl.closedCount).toBe(1);
    expect(dry.pnl.realizedSol).toBeCloseTo(0.004, 9);
    expect(dry.positions.openCount).toBe(1);

    const live = getDashboardSummary(db, config, { track: 'live' });
    expect(live.pnl.closedCount).toBe(0);
    expect(live.positions.openCount).toBe(0);

    expect(listPositions(db, { track: 'dry', mode: 'dry-run', state: 'all' }).map((p) => p.mint).sort()).toEqual(['OPENSIM', 'SIM']);
    expect(listPositions(db, { track: 'live', mode: 'dry-run', state: 'all' })).toHaveLength(0);
  });

  it('live and paper bots keep live = own rows, dry = the paper twin', () => {
    expect(trackSource('live', 'live')).toMatchObject({ table: 'positions', mode: 'live', isTwin: false });
    expect(trackSource('dry', 'live')).toMatchObject({ table: 'dry_run_positions', isTwin: true });
    expect(trackSource('live', 'paper')).toMatchObject({ table: 'positions', mode: 'paper' });
  });
});
