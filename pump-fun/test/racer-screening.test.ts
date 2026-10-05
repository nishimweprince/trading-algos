import { describe, expect, it } from 'vitest';
import { ConfigSchema, type Config } from '../src/config/schema.ts';
import { openDb } from '../src/persistence/db.ts';
import { Repositories } from '../src/persistence/repositories.ts';
import {
  candidateFromRacer,
  screenRacerTrack,
  SCREENING_ENGINE,
  type RacerScreeningInput,
} from '../src/racer/screening.ts';
import { buildRacerPaperCsv, getRacerSummary } from '../src/dashboard/queries.ts';

const MINT = 'TestMint1111111111111111111111111111111pump';

function cfg(): Config {
  return ConfigSchema.parse({ mode: 'paper' });
}

function repos(): Repositories {
  return new Repositories(openDb({ path: ':memory:', memory: true }));
}

function input(over: Partial<RacerScreeningInput> = {}): RacerScreeningInput {
  return {
    mint: MINT, poolAddress: 'Pool1111111111111111111111111111111111', migrateSlot: 100,
    reachQuoteSol: 60, quoteVaultSol: 30, coinCreator: '', isToken2022: false, ...over,
  };
}

describe('screenRacerTrack', () => {
  it('accepts a deep, clean, canonical-by-construction migration', () => {
    const v = screenRacerTrack(input(), cfg(), repos())!;
    expect(v.verdict).toBe('accept');
    expect(v.fails).toEqual([]);
    expect(v.engine).toBe(SCREENING_ENGINE);
    expect(v.synthesized).toBe(true);
    expect(v.softScore).toBeGreaterThan(0);
  });

  it('vetoes H7 when the vault misses the floor', () => {
    const v = screenRacerTrack(input({ quoteVaultSol: 10, reachQuoteSol: 40 }), cfg(), repos())!;
    expect(v.verdict).toBe('veto');
    expect(v.fails[0]!.id).toBe('H7');
  });

  it('vetoes H7 on buy impact against a thin effective reserve', () => {
    const c = cfg();
    c.guardrails.maxBuyImpactPct = 0.01;
    // Vault clears the 25 SOL floor, but the 0.01 SOL base buy impacts 30.5 SOL by 0.033%.
    const v = screenRacerTrack(input({ quoteVaultSol: 30, reachQuoteSol: 30.5 }), c, repos())!;
    expect(v.verdict).toBe('veto');
    expect(v.fails[0]!.id).toBe('H7');
    expect(v.fails[0]!.detail).toContain('impact');
  });

  it('vetoes H12 for non-pump mints when the population filter is on', () => {
    const c = cfg();
    c.guardrails.population = { enabled: true, requirePumpSuffix: true };
    const v = screenRacerTrack(input({ mint: 'So11111111111111111111111111111111111111112' }), c, repos())!;
    expect(v.verdict).toBe('veto');
    expect(v.fails.map((f) => f.id)).toContain('H12');
    expect(screenRacerTrack(input(), c, repos())!.verdict).toBe('accept');
  });

  it('vetoes H8 for a blacklisted mint or creator, and skips the creator clause when unknown', () => {
    const r = repos();
    r.blacklistMint(MINT, 'test');
    expect(screenRacerTrack(input(), cfg(), r)!.fails.map((f) => f.id)).toContain('H8');
    const r2 = repos();
    r2.blacklistCreator('Creator1111111111111111111111111111111111', 'test');
    expect(screenRacerTrack(input({ coinCreator: 'Creator1111111111111111111111111111111111' }), cfg(), r2)!.fails.map((f) => f.id))
      .toContain('H8');
    // Unknown creator: the mint clause still runs, the creator clause cannot fail.
    expect(screenRacerTrack(input({ coinCreator: '' }), cfg(), r2)!.verdict).toBe('accept');
  });

  it('is null without H7 inputs (legacy tracks stay unscreened, never guessed)', () => {
    expect(screenRacerTrack(input({ quoteVaultSol: null }), cfg(), repos())).toBeNull();
    expect(screenRacerTrack(input({ reachQuoteSol: 0 }), cfg(), repos())).toBeNull();
    expect(candidateFromRacer(input({ quoteVaultSol: null }))).toBeNull();
  });

  it('derives virtual reserves as effective minus vault, clamped at zero', () => {
    const c = candidateFromRacer(input({ quoteVaultSol: 30, reachQuoteSol: 60 }))!;
    expect(c.enrichment.pool!.quoteReserveLamports).toBe(30_000_000_000n);
    expect(c.enrichment.pool!.virtualQuoteLamports).toBe(30_000_000_000n);
    const corrupt = candidateFromRacer(input({ quoteVaultSol: 60, reachQuoteSol: 30 }))!;
    expect(corrupt.enrichment.pool!.virtualQuoteLamports).toBe(0n);
  });
});

describe('screening in the dashboard', () => {
  function seed(): ReturnType<typeof openDb> {
    const db = openDb({ path: ':memory:', memory: true });
    const horizons = JSON.stringify([{ sec: 60, pnl: { slot1mid: 5 }, exitPrice: 1, exitQuoteSol: 10 }]);
    const entries = JSON.stringify({ slot1mid: { price: 1, quoteSol: 10 } });
    const ins = db.prepare(`INSERT INTO racer_paper_tracks
      (mint, size_sol, reach_price, reach_quote_sol, horizons_json, entries_json, created_at, armed,
       entry_model_version, entry_quality, screening_json)
      VALUES (?, 0.1, 1, 10, ?, ?, ?, 1, 2, 'verified', ?)`);
    ins.run('A', horizons, entries, '2026-01-01 00:00:00',
      JSON.stringify({ engine: SCREENING_ENGINE, verdict: 'accept', fails: [], softScore: 65, synthesized: true }));
    ins.run('B', horizons, entries, '2026-01-01 00:01:05',
      JSON.stringify({ engine: SCREENING_ENGINE, verdict: 'veto', fails: [{ id: 'H7', detail: 'reserve' }], softScore: 40, synthesized: true }));
    ins.run('C', horizons, entries, '2026-01-01 00:02:05', null);
    return db;
  }

  it('counts the screened cohort in the summary', () => {
    const s = getRacerSummary(seed(), {}).screening;
    expect(s).toEqual({ tracks: 3, screened: 2, accepted: 1, vetoed: 1, unknown: 1, vetoesByCheck: { H7: 1 } });
  });

  it('carries the verdict JSON on each CSV row', () => {
    const lines = buildRacerPaperCsv(seed(), {}).trim().split('\n');
    expect(lines[0]!.split(',')).toContain('screening_json');
    expect(lines.join('\n')).toContain('accept');
  });
});
