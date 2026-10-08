import { describe, expect, it } from 'vitest';
import { PublicKey } from '@solana/web3.js';
import { canonicalPumpPoolPda } from '@pump-fun/pump-swap-sdk';
import { PROGRAM_IDS } from '../src/core/constants.ts';
import { decodeTransferFee } from '../src/enrichment/mint.ts';
import {
  advContextFromFeatures,
  dumpedExit,
  isCanonicalPool,
  t22FeePct,
} from '../src/racer/adversarial.ts';
import { roundTripParts } from '../src/racer/economics.ts';
import { recostPnlForDelta, PaperTracker, type EntryFeatureSnapshot, type PaperTrack } from '../src/racer/paper.ts';
import { ConfigSchema } from '../src/config/schema.ts';
import { openDb } from '../src/persistence/db.ts';
import { Repositories } from '../src/persistence/repositories.ts';
import { screenRacerTrack } from '../src/racer/screening.ts';
import { buildRacerPaperCsv } from '../src/dashboard/queries.ts';

const SPL = PROGRAM_IDS.TOKEN;
const T22 = PROGRAM_IDS.TOKEN_2022;

function features(over: Partial<EntryFeatureSnapshot> = {}): EntryFeatureSnapshot {
  return {
    version: 1, observedAtMs: 0, migrateSlot: 1, fireToCompleteSlots: null, completeToMigrateSlots: null,
    caller: null, quoteVaultSol: 30, tokenProgram: SPL, sameSlotDepletionPct: null, knownFailedMigrateAttempts: 0,
    bundledTokens: '0', poolCanonical: true, ...over,
  };
}

describe('advContextFromFeatures', () => {
  it('resolves explicit inputs, including zero bundle and clean T22', () => {
    const c = advContextFromFeatures(features({ bundledTokens: '1000' }));
    expect(c.blocked).toBe(false);
    if (!c.blocked) {
      expect(c.bundledRaw).toBe(1000n);
      expect(c.transferFee).toBeNull();
    }
    const clean = advContextFromFeatures(features({ tokenProgram: T22, transferFee: null }));
    expect(clean.blocked).toBe(false);
    const fee = advContextFromFeatures(features({ tokenProgram: T22, transferFee: { bps: 50, maxRaw: '999' } }));
    if (!fee.blocked) expect(fee.transferFee).toEqual({ bps: 50, maxRaw: 999n });
    else expect.unreachable();
  });

  it('blocks unknown inputs fail-open, never as zero', () => {
    expect(advContextFromFeatures(null)).toMatchObject({ blocked: true, reason: 'bundle_unknown' });
    const noBundleKey = features();
    delete noBundleKey.bundledTokens;
    expect(advContextFromFeatures(noBundleKey)).toMatchObject({ blocked: true, reason: 'bundle_unknown' });
    expect(advContextFromFeatures(features({ bundledTokens: 'zzz' }))).toMatchObject({ blocked: true, reason: 'bundle_unknown' });
    expect(advContextFromFeatures(features({ poolCanonical: false }))).toMatchObject({ blocked: true, reason: 'non_canonical_pool' });
    expect(advContextFromFeatures(features({ tokenProgram: null }))).toMatchObject({ blocked: true, reason: 't22_fee_unknown' });
    expect(advContextFromFeatures(features({ tokenProgram: T22 }))).toMatchObject({ blocked: true, reason: 't22_fee_unknown' });
    expect(advContextFromFeatures(features({ tokenProgram: T22, transferFee: 'unknown' })))
      .toMatchObject({ blocked: true, reason: 't22_fee_unknown' });
  });
});

describe('dumpedExit', () => {
  it('reprices the quote after the bundle sells ahead of us', () => {
    const { exit, drained } = dumpedExit({ price: 1, quoteSol: 100 }, 10_000_000n);
    expect(drained).toBe(false);
    // 10 SOL of bundle takes 10/1.1 SOL out of a 100 SOL pool.
    expect(exit.quoteSol).toBeCloseTo(100 - 10 / 1.1, 12);
  });

  it('drains the vault cap with the quote and flags an empty vault', () => {
    const { exit, drained } = dumpedExit({ price: 1, quoteSol: 100, realQuoteSol: 50 }, 10_000_000n);
    expect(drained).toBe(false);
    expect(exit.realQuoteSol).toBeCloseTo(50 - 10 / 1.1, 12);
    const thin = dumpedExit({ price: 1, quoteSol: 100, realQuoteSol: 5 }, 10_000_000n);
    expect(thin.drained).toBe(true);
    expect(thin.exit.realQuoteSol).toBe(0);
  });

  it('leaves the exit alone without a bundle', () => {
    const exit = { price: 1, quoteSol: 100 };
    expect(dumpedExit(exit, 0n)).toEqual({ exit, drained: false });
  });
});

describe('t22FeePct', () => {
  it('charges bps on proceeds, capped at maxFee value', () => {
    expect(t22FeePct(1.0, 2, { bps: 100, maxRaw: 10n ** 30n }, 0.1)).toBeCloseTo(10, 12);
    // maxFee 0.001 tokens at 2 SOL/token = 0.002 SOL cap < 0.01 proportional.
    expect(t22FeePct(1.0, 2, { bps: 100, maxRaw: 1_000n }, 0.1)).toBeCloseTo(2, 12);
    expect(t22FeePct(0, 2, { bps: 100, maxRaw: 1n }, 0.1)).toBe(0);
  });
});

describe('isCanonicalPool', () => {
  const mint = 'So11111111111111111111111111111111111111112';
  it('matches the derived PDA and rejects anything else', () => {
    const pda = canonicalPumpPoolPda(new PublicKey(mint)).toBase58();
    expect(isCanonicalPool(mint, pda)).toBe(true);
    expect(isCanonicalPool(mint, 'Pool1111111111111111111111111111111111111111')).toBe(false);
    expect(isCanonicalPool('not-a-mint', pda)).toBeNull();
    expect(isCanonicalPool(mint, null)).toBeNull();
  });
});

describe('decodeTransferFee', () => {
  function mintWithFee(bps: number, maxRaw: bigint): string {
    const buf = Buffer.alloc(166 + 4 + 108);
    buf.writeUInt16LE(1, 166); // extension type 1 = TransferFeeConfig
    buf.writeUInt16LE(108, 168);
    buf.writeBigUInt64LE(maxRaw, 170 + 98); // newer maximumFee
    buf.writeUInt16LE(bps, 170 + 106); // newer bps
    return buf.toString('base64');
  }

  it('reads the newer-epoch fee, and nulls everything else', () => {
    expect(decodeTransferFee(mintWithFee(50, 999n), T22)).toEqual({ bps: 50, maxRaw: 999n });
    expect(decodeTransferFee(mintWithFee(50, 999n), SPL)).toBeNull();
    expect(decodeTransferFee(Buffer.alloc(200).toString('base64'), T22)).toBeNull();
    expect(decodeTransferFee(Buffer.alloc(82).toString('base64'), T22)).toBeNull();
  });
});

describe('adversarial legs in the tracker', () => {
  async function runTrack(entryFeatures: EntryFeatureSnapshot | null): Promise<PaperTrack> {
    const sunk: PaperTrack[] = [];
    const paper = new PaperTracker(
      {
        readMark: async () => ({ price: 1.1, quoteSol: 10 }),
        sink: (t) => sunk.push(t),
        schedule: (_ms, fn) => void fn(),
      },
      { horizonsSec: [60], sizeSol: 0.1, liveCostSol: 0, exitLandMs: 0 },
    );
    paper.track('M', {
      poolAddress: 'P', slot: 1, open: null, reach: { price: 1, quoteSol: 10 },
      delayed: { slot1: { price: 1, quoteSol: 10 } },
      entryFeatures,
    });
    for (let i = 0; i < 100 && paper.pendingCount > 0; i++) await new Promise((r) => setImmediate(r));
    expect(paper.pendingCount).toBe(0);
    return sunk[sunk.length - 1]!;
  }

  it('haircuts the exit after the bundle dumps', async () => {
    const t = await runTrack(features({ bundledTokens: '5000000' })); // 5 tokens
    const h = t.horizons[0]!;
    const exit = { price: 1.1, quoteSol: 10 };
    const expected = roundTripParts({ price: 1, quoteSol: 10 }, dumpedExit(exit, 5_000_000n).exit, 0.1)!.pct;
    expect(h.pnl.slot1_adv).toBeCloseTo(expected, 9);
    expect(h.pnl.slot1_adv!).toBeLessThan(h.pnl.slot1!);
    expect(h.advReasons).toBeUndefined();
  });

  it('blocks T22 with an unverified fee, and prices it once read', async () => {
    const unknown = await runTrack(features({ tokenProgram: T22 }));
    expect(unknown.horizons[0]!.pnl.slot1_adv).toBeUndefined();
    expect(unknown.horizons[0]!.advReasons).toEqual({ reach: 't22_fee_unknown', slot1: 't22_fee_unknown' });
    const clean = await runTrack(features({ tokenProgram: T22, transferFee: null, bundledTokens: '0' }));
    // Proven clean + empty bundle: the adversarial leg equals the base leg exactly.
    expect(clean.horizons[0]!.pnl.slot1_adv).toBe(clean.horizons[0]!.pnl.slot1);
    const fee = await runTrack(features({
      tokenProgram: T22, transferFee: { bps: 100, maxRaw: `${10n ** 30n}` }, bundledTokens: '0',
    }));
    // 1% fee on proceeds lands strictly inside the base leg.
    expect(fee.horizons[0]!.pnl.slot1_adv!).toBeLessThan(fee.horizons[0]!.pnl.slot1!);
    expect(fee.horizons[0]!.advReasons).toBeUndefined();
  });

  it('blocks non-canonical pools and legacy tracks without features', async () => {
    const nonCanon = await runTrack(features({ poolCanonical: false }));
    expect(nonCanon.horizons[0]!.pnl.slot1_adv).toBeUndefined();
    expect(nonCanon.horizons[0]!.advReasons!.slot1).toBe('non_canonical_pool');
    const legacy = await runTrack(null);
    expect(legacy.horizons[0]!.pnl.slot1_adv).toBeUndefined();
    expect(legacy.horizons[0]!.advReasons!.slot1).toBe('bundle_unknown');
  });
});

describe('recostPnlForDelta adversarial legs', () => {
  it('shifts _adv legs whole (their haircut carries no live cost)', () => {
    const pnl: Record<string, number | null> = { slot1: 100, slot1_adv: 90 };
    recostPnlForDelta(pnl, {}, ['slot1'], 2);
    expect(pnl.slot1).toBe(98);
    expect(pnl.slot1_adv).toBe(88);
  });
});

describe('screening premise-break detection', () => {
  const base = {
    mint: 'TestMint1111111111111111111111111111111pump', poolAddress: 'P', migrateSlot: 100,
    reachQuoteSol: 60, quoteVaultSol: 30, coinCreator: '',
  };

  it('vetoes a detected transfer fee and fails closed on an unverified T22 mint', () => {
    const config = ConfigSchema.parse({ mode: 'paper' });
    const db = openDb({ path: ':memory:', memory: true });
    const repos = new Repositories(db);
    const fee = screenRacerTrack({ ...base, isToken2022: true, transferFee: { bps: 50, maxRaw: '999' } }, config, repos)!;
    expect(fee.verdict).toBe('veto');
    expect(fee.fails[0]).toMatchObject({ id: 'P0', reason: 'rug_extension' });
    const unread = screenRacerTrack({ ...base, isToken2022: true, transferFee: 'unknown' }, config, repos)!;
    expect(unread.verdict).toBe('veto');
    expect(unread.fails[0]).toMatchObject({ id: 'P0', reason: 't22_fee_unreadable' });
    expect(screenRacerTrack({ ...base, isToken2022: true, transferFee: null }, config, repos)!.verdict).toBe('accept');
    expect(screenRacerTrack({ ...base, isToken2022: false }, config, repos)!.verdict).toBe('accept');
  });
});

describe('paper CSV adversarial reasons', () => {
  it('carries adv_reasons_json on horizon rows', () => {
    const db = openDb({ path: ':memory:', memory: true });
    db.prepare(`INSERT INTO racer_paper_tracks
      (mint, size_sol, reach_price, reach_quote_sol, horizons_json, entries_json, created_at, armed,
       entry_model_version, entry_quality)
      VALUES ('A', 0.1, 1, 10, '[{"sec":60,"pnl":{"slot1":5},"advReasons":{"slot1":"bundle_unknown"}}]', '{}',
       '2026-01-01 00:00:00', 1, 2, 'verified')`).run();
    const csv = buildRacerPaperCsv(db, {});
    expect(csv.split('\n')[0]!.split(',')).toContain('adv_reasons_json');
    expect(csv).toContain('bundle_unknown');
  });
});
