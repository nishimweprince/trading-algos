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
});
