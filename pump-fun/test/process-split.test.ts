import { describe, expect, it } from 'vitest';
import { openDb } from '../src/persistence/db.ts';
import { Repositories } from '../src/persistence/repositories.ts';
import { candidateFromRecorded } from '../src/research-worker.ts';
import { resolveRole } from '../src/core/role.ts';
import type { CandidateVerdict } from '../src/core/types.ts';

describe('process split (trader / research)', () => {
  it('resolves the role from PUMPDESK_ROLE over the config', () => {
    expect(resolveRole(undefined, 'all')).toBe('all');
    expect(resolveRole('trader', 'all')).toBe('trader');
    expect(() => resolveRole('both', 'all')).toThrow(/PUMPDESK_ROLE/);
  });

  it('the research worker rebuilds the verdict + candidate the trader recorded', () => {
    const repos = new Repositories(openDb({ path: ':memory:', memory: true }));
    const start = repos.maxRowid('candidates');
    const verdict: CandidateVerdict = {
      mint: 'M', verdict: 'veto', softScore: 65, vetoReasons: ['H13'], highVolatility: false, sizeMultiplier: 0,
      hardChecks: [{ id: 'H13', label: 'cluster', status: 'fail', detail: 'x' }],
    };
    const enrichment = {
      unknowns: [], elapsedMs: 3,
      pool: { poolAddress: 'P', baseMint: 'M', quoteMint: 'W', lpMint: 'L', baseVault: 'B', quoteVault: 'Q', creator: 'C', coinCreator: 'CC',
        isCanonical: true, baseReserve: '206900000000000', quoteReserveLamports: '67405855261', virtualQuoteLamports: '17584505324' },
    };
    repos.recordVerdict(verdict, JSON.stringify(enrichment));
    const rows = repos.candidatesAfter(start);
    expect(rows).toHaveLength(1);
    const built = candidateFromRecorded(rows[0]!)!;
    expect(built.verdict).toMatchObject({ mint: 'M', verdict: 'veto', vetoReasons: ['H13'], softScore: 65 });
    expect(built.verdict.hardChecks[0]!.id).toBe('H13');
    expect(built.candidate.enrichment.pool?.virtualQuoteLamports).toBe(17_584_505_324n);
    expect(repos.candidatesAfter(rows[0]!.rowid)).toHaveLength(0);
  });
});
