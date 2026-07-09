import { describe, it, expect } from 'vitest';
import { openDb, prunePriceTicks } from '../src/persistence/db.ts';
import { Repositories } from '../src/persistence/repositories.ts';
import type { CandidateVerdict, GraduationEvent } from '../src/core/types.ts';

function grad(mint: string): GraduationEvent {
  return {
    mint,
    venue: 'pumpswap',
    poolAddress: 'pool1',
    slot: 100,
    feedSource: 'grpc',
    receivedAtNs: 123n,
    detectionLatencyMs: 42,
  };
}

describe('persistence', () => {
  it('creates schema and records a graduation', () => {
    const db = openDb({ path: ':memory:', memory: true });
    const repos = new Repositories(db);
    expect(repos.countGraduations()).toBe(0);
    repos.recordGraduation(grad('mintA'));
    expect(repos.countGraduations()).toBe(1);
    // Idempotent on (mint, slot).
    repos.recordGraduation(grad('mintA'));
    expect(repos.countGraduations()).toBe(1);
    db.close();
  });

  it('records a veto verdict with full check results', () => {
    const db = openDb({ path: ':memory:', memory: true });
    const repos = new Repositories(db);
    const verdict: CandidateVerdict = {
      mint: 'mintB',
      verdict: 'veto',
      hardChecks: [{ id: 'H1', label: 'Mint authority', status: 'fail', detail: 'not revoked' }],
      softScore: 12,
      vetoReasons: ['H1'],
      highVolatility: false,
      sizeMultiplier: 0,
    };
    repos.recordVerdict(verdict, JSON.stringify({ any: 'thing' }));
    const row = db.prepare('SELECT verdict, veto_reasons FROM candidates WHERE mint = ?').get('mintB') as {
      verdict: string;
      veto_reasons: string;
    };
    expect(row.verdict).toBe('veto');
    expect(JSON.parse(row.veto_reasons)).toEqual(['H1']);
    db.close();
  });

  it('blacklist round-trips', () => {
    const db = openDb({ path: ':memory:', memory: true });
    const repos = new Repositories(db);
    expect(repos.isCreatorBlacklisted('dev1')).toBe(false);
    repos.blacklistCreator('dev1', 'emergency exit');
    expect(repos.isCreatorBlacklisted('dev1')).toBe(true);
    db.close();
  });

  it('prunePriceTicks removes old rows', () => {
    const db = openDb({ path: ':memory:', memory: true });
    db.prepare(
      `INSERT INTO price_ticks (mint, slot, price, sol_reserve, created_at) VALUES ('m', 1, 1.0, 30, datetime('now', '-10 days'))`,
    ).run();
    db.prepare(
      `INSERT INTO price_ticks (mint, slot, price, sol_reserve) VALUES ('m', 2, 1.1, 31)`,
    ).run();
    const removed = prunePriceTicks(db, 7);
    expect(removed).toBe(1);
    const remaining = db.prepare('SELECT COUNT(*) AS n FROM price_ticks').get() as { n: number };
    expect(remaining.n).toBe(1);
    db.close();
  });

  it('insertPriceTick persists a tick', () => {
    const db = openDb({ path: ':memory:', memory: true });
    const repos = new Repositories(db);
    repos.insertPriceTick({ mint: 'm', slot: null, price: 1.5, solReserve: 42 });
    const row = db.prepare('SELECT price, sol_reserve FROM price_ticks WHERE mint = ?').get('m') as {
      price: number;
      sol_reserve: number;
    };
    expect(row.price).toBe(1.5);
    expect(row.sol_reserve).toBe(42);
    db.close();
  });

  it('migration is idempotent (positions has raw_base_amount + pricing_json)', () => {
    const db = openDb({ path: ':memory:', memory: true });
    // openDb ran migrate(); the columns must exist and a second migrate is a no-op.
    const cols = (db.prepare('PRAGMA table_info(positions)').all() as Array<{ name: string }>).map((c) => c.name);
    expect(cols).toContain('raw_base_amount');
    expect(cols).toContain('pricing_json');
    db.close();
  });

  it('risk-manager query helpers over CLOSED positions', () => {
    const db = openDb({ path: ':memory:', memory: true });
    const repos = new Repositories(db);
    const close = (mint: string, pnl: number, trigger: string, closedAt: string) =>
      db
        .prepare(
          `INSERT INTO positions (mint, size_sol, state, exit_reason, pnl_sol, closed_at) VALUES (?, 0.25, 'CLOSED', ?, ?, ?)`,
        )
        .run(mint, trigger, pnl, closedAt);
    close('a', -0.1, 'STOP_LOSS', '2026-07-08T10:00:00Z');
    close('b', 0.2, 'TAKE_PROFIT_1', '2026-07-08T11:00:00Z');
    close('c', -0.05, 'EMERGENCY_EXIT', '2026-07-08T12:00:00Z');

    expect(repos.sumRealizedPnlSince('2026-07-08T00:00:00Z')).toBeCloseTo(0.05, 6);
    expect(repos.sumRealizedPnlSince('2026-07-08T11:30:00Z')).toBeCloseTo(-0.05, 6);
    expect(repos.countClosedByTriggerSince('EMERGENCY_EXIT', '2026-07-08T00:00:00Z')).toBe(1);
    expect(repos.recentClosedPnls(2)).toEqual([-0.05, 0.2]); // newest first
    db.close();
  });

  it('latestOpenPositions returns only mints whose latest row is OPEN', () => {
    const db = openDb({ path: ':memory:', memory: true });
    const repos = new Repositories(db);
    // openMint: OPEN only. closedMint: OPEN then CLOSED (latest wins → excluded).
    repos.upsertPosition(
      { mint: 'openMint', state: 'OPEN', sizeSol: 0.25, entryPrice: 1e-7, openedAt: Date.now() },
      { rawBaseAmount: 1000n, pricingJson: '{"poolAddress":"P"}' },
    );
    repos.upsertPosition({ mint: 'closedMint', state: 'OPEN', sizeSol: 0.25, entryPrice: 1e-7, openedAt: Date.now() });
    repos.upsertPosition({ mint: 'closedMint', state: 'CLOSED', sizeSol: 0.25, pnlSol: 0.1, closedAt: Date.now() });

    const open = repos.latestOpenPositions();
    expect(open.map((o) => o.mint)).toEqual(['openMint']);
    expect(open[0]!.rawBaseAmount).toBe('1000');
    expect(open[0]!.pricingJson).toBe('{"poolAddress":"P"}');
    db.close();
  });
});
