import { describe, it, expect } from 'vitest';
import { effectiveHolderShares } from '../src/enrichment/holderShares.ts';
import { extractStrategyFeatures } from '../src/dashboard/features.ts';
import { backfillHolderShares } from '../src/persistence/backfillHolderShares.ts';
import { openDb } from '../src/persistence/db.ts';
import { deriveBondingCurvePda } from '../src/enrichment/curve.ts';
import type { EnrichmentData } from '../src/enrichment/types.ts';
import type { PoolInfo } from '../src/enrichment/pool.ts';

// 5tCju…pump (2026-09-28): raw top10 48.9 % / max 20.7 % (the base vault);
// H5 saw top10 30.4 % / max 4.0 %.
const MINT = '5tCju6YNxHq5zrA6tGndr6F7TK42mpUFmeE31cSFpump';
const CURVE = deriveBondingCurvePda(MINT)!;

const pool = {
  poolAddress: 'pool',
  baseMint: MINT,
  quoteMint: 'So11111111111111111111111111111111111111112',
  lpMint: 'lp',
  baseVault: 'baseVault',
  quoteVault: 'quoteVault',
  creator: 'poolCreator',
  coinCreator: 'creator',
  isCanonical: true,
  baseReserve: 1_000_000_000_000n,
  quoteReserveLamports: 68_600_000_000n,
  lpMintSupply: 0n,
} as PoolInfo;

const list = [
  { account: 'baseVault', owner: 'poolAuthority', amount: 0n, share: 0.2068 },
  { account: 'curveAta', owner: CURVE, amount: 0n, share: 0.05 },
  { account: 'w1', owner: 'W1', amount: 0n, share: 0.04 },
  ...Array.from({ length: 11 }, (_, i) => ({ account: `h${i}`, owner: `H${i}`, amount: 0n, share: 0.03 })),
];
const holders = { supply: 1n, decimals: 6, holders: list, top10Share: 0.4889, maxShare: 0.2068 };

describe('effective holder shares', () => {
  it('excludes the base vault and bonding curve (H5 definition)', () => {
    const s = effectiveHolderShares(holders, pool, MINT, false)!;
    expect(s.maxShare).toBeCloseTo(0.04, 9);
    expect(s.top10Share).toBeCloseTo(0.04 + 9 * 0.03, 9);
    expect(s.real.map((h) => h.account)).not.toContain('baseVault');
  });

  it('persisted features use the vault-excluded values, not the raw snapshot', () => {
    const e = { unknowns: [], elapsedMs: 0, pool, holders } as EnrichmentData;
    const f = extractStrategyFeatures(e);
    expect(f.maxHolderShare).toBeCloseTo(0.04, 9);
    expect(f.top10Share).toBeCloseTo(0.31, 9);
  });

  it('records unknown (not the vault-dominated raw value) without a pool', () => {
    const f = extractStrategyFeatures({ unknowns: [], elapsedMs: 0, holders } as EnrichmentData);
    expect(f.top10Share).toBeNull();
    expect(f.maxHolderShare).toBeNull();
  });
});

describe('backfillHolderShares', () => {
  // Stored the way safeJson persists enrichment: bigints as strings.
  const stored = list.map((h) => ({ ...h, amount: h.amount.toString() }));
  it('rewrites raw candidate + position columns from enrichment_json, idempotently', () => {
    const db = openDb({ path: ':memory:', memory: true });
    const enrichment = JSON.stringify({ pool: { baseVault: 'baseVault', quoteVault: 'quoteVault', baseMint: MINT }, holders: { holders: stored } });
    db.prepare(`INSERT INTO candidates (mint, enrichment_json, top10_share, max_holder_share) VALUES (?, ?, 0.4889, 0.2068)`).run(MINT, enrichment);
    db.prepare(`INSERT INTO candidates (mint, enrichment_json, top10_share, max_holder_share) VALUES ('NoPool', ?, 0.5, 0.2)`).run(
      JSON.stringify({ holders: { holders: stored } }),
    );
    db.prepare(`INSERT INTO positions (mint, state, top10_share, max_holder_share) VALUES (?, 'CLOSED', 0.4889, 0.2068)`).run(MINT);

    const first = backfillHolderShares(db);
    expect(first).toEqual({ candidates: 2, positions: 1, nulled: 1 });
    const cand = db.prepare(`SELECT top10_share t, max_holder_share m FROM candidates WHERE mint = ?`).get(MINT) as { t: number; m: number };
    expect(cand.m).toBeCloseTo(0.04, 9);
    expect(cand.t).toBeCloseTo(0.31, 9);
    const pos = db.prepare(`SELECT top10_share t, max_holder_share m FROM positions WHERE mint = ?`).get(MINT) as { t: number; m: number };
    expect(pos).toEqual(cand);
    expect(db.prepare(`SELECT top10_share t FROM candidates WHERE mint = 'NoPool'`).get()).toEqual({ t: null });

    backfillHolderShares(db);
    expect(db.prepare(`SELECT top10_share t, max_holder_share m FROM candidates WHERE mint = ?`).get(MINT)).toEqual(cand);
  });
});
