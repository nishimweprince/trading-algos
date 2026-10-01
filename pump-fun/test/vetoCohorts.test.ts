import { describe, it, expect } from 'vitest';
import {
  buildReport,
  cohortReason,
  poolBand,
  type VetoRow,
} from '../src/research/vetoCohorts.ts';

const row = (over: Partial<VetoRow> = {}): VetoRow => ({
  mint: over.mint ?? 'm',
  primaryVetoCode: over.primaryVetoCode ?? null,
  vetoCodes: over.vetoCodes ?? [],
  h12Reason: over.h12Reason ?? null,
  poolSol: over.poolSol ?? null,
  netPnlSol: over.netPnlSol ?? 0,
  pnlPct: over.pnlPct ?? 0,
  exitReason: over.exitReason ?? null,
});

describe('vetoCohorts bucketing', () => {
  it('bands pool SOL at 25/60/90/300', () => {
    expect(poolBand(null)).toBe('unknown');
    expect(poolBand(24.9)).toBe('<25');
    expect(poolBand(25)).toBe('25-60');
    expect(poolBand(59.9)).toBe('25-60');
    expect(poolBand(60)).toBe('60-90');
    expect(poolBand(70)).toBe('60-90');
    expect(poolBand(90)).toBe('90-300');
    expect(poolBand(300)).toBe('90-300');
    expect(poolBand(300.1)).toBe('>300');
  });

  it('uses the H12 sub-reason when H12 is primary, else the primary code', () => {
    expect(cohortReason(row({ primaryVetoCode: 'H12', h12Reason: 'mint_age_unknown' }))).toBe('mint_age_unknown');
    expect(cohortReason(row({ primaryVetoCode: 'H12' }))).toBe('H12');
    expect(cohortReason(row({ primaryVetoCode: 'H5' }))).toBe('H5');
  });

  it('lands fixture rows in the right cohorts, including in-band by check', () => {
    const rows = [
      row({ mint: 'a', primaryVetoCode: 'H12', vetoCodes: ['H12'], h12Reason: 'mint_age_unknown', poolSol: 70 }),
      row({ mint: 'b', primaryVetoCode: 'H5', vetoCodes: ['H5'], poolSol: 70 }),
      row({ mint: 'c', primaryVetoCode: 'H7', vetoCodes: ['H7'], poolSol: 0.03 }),
    ];
    const r = buildReport(rows, { outcomeVersion: 'exit_fsm_v3_amm', range: '7d' });
    const keys = r.cohorts.map((c) => c.key);
    expect(keys).toContain('mint_age_unknown | 60-90');
    expect(keys).toContain('H5 | 60-90');
    expect(keys).toContain('H7 | <25');
    expect(r.inBandByCheck.map((c) => c.key)).toContain('H5 | 60-90');
    expect(r.inBandByCheck.map((c) => c.key)).not.toContain('H7 | <25');
  });

  it('flags a relax candidate only at n >= 30 with CI lower bound > 0', () => {
    const winners = Array.from({ length: 30 }, (_, i) => row({ mint: `w${i}`, pnlPct: 1, netPnlSol: 0.01 }));
    const [flagged] = buildReport(winners, { outcomeVersion: 'v', range: '7d' }).cohorts;
    expect(flagged!.n).toBe(30);
    expect(flagged!.relaxCandidate).toBe(true);

    const few = winners.slice(0, 29);
    expect(buildReport(few, { outcomeVersion: 'v', range: '7d' }).cohorts[0]!.relaxCandidate).toBe(false);

    const mixed = [
      ...Array.from({ length: 15 }, (_, i) => row({ mint: `p${i}`, pnlPct: 1, netPnlSol: 0.01 })),
      ...Array.from({ length: 15 }, (_, i) => row({ mint: `q${i}`, pnlPct: -1, netPnlSol: -0.01 })),
    ];
    const [m] = buildReport(mixed, { outcomeVersion: 'v', range: '7d' }).cohorts;
    expect(m!.n).toBe(30);
    expect(m!.ciLo).toBeLessThanOrEqual(0);
    expect(m!.relaxCandidate).toBe(false);
  });
});
