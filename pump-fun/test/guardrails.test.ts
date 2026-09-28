import { describe, it, expect } from 'vitest';
import { PublicKey } from '@solana/web3.js';
import { canonicalPumpPoolPda } from '@pump-fun/pump-swap-sdk';
import { base58Encode, base58Decode } from '../src/core/base58.ts';
import { decodeMint, MintExtension } from '../src/enrichment/mint.ts';
import { PROGRAM_IDS, WSOL_MINT } from '../src/core/constants.ts';
import { deriveAta } from '../src/core/ata.ts';
import { GuardrailEngine } from '../src/guardrails/engine.ts';
import { FastPoolReader, reservesFromTx } from '../src/guardrails/fastRead.ts';
import { GuardrailPipeline } from '../src/guardrails/pipeline.ts';
import { reviveEnrichment, replayRow, candidateFromRow } from '../src/research/screenReplay.ts';
import { TypedBus } from '../src/core/bus.ts';
import type { RpcClient } from '../src/core/rpc.ts';
import type { CandidateVerdict } from '../src/core/types.ts';
import { scoreCandidate, sizeMultiplierFor, momentumSizeFactor, DEFAULT_MOMENTUM_OPTS } from '../src/guardrails/scoring.ts';
import { computeEarlyFlow } from '../src/enrichment/momentum.ts';
import { ConfigSchema } from '../src/config/schema.ts';
import { openDb } from '../src/persistence/db.ts';
import { Repositories } from '../src/persistence/repositories.ts';
import type { Candidate } from '../src/enrichment/types.ts';
import type { GraduationEvent } from '../src/core/types.ts';
import type { PoolInfo } from '../src/enrichment/pool.ts';

const PUBKEY = base58Encode(Buffer.alloc(32).fill(7)); // deterministic nonzero pubkey

function buildMintBase64(opts: {
  mintAuth?: string | null;
  freezeAuth?: string | null;
  supply?: bigint;
  decimals?: number;
  token2022?: boolean;
  extensions?: number[];
}): string {
  const base = Buffer.alloc(82);
  if (opts.mintAuth) {
    base.writeUInt32LE(1, 0);
    Buffer.from(base58Decode(opts.mintAuth)).copy(base, 4);
  }
  base.writeBigUInt64LE(opts.supply ?? 1000n, 36);
  base.writeUInt8(opts.decimals ?? 6, 44);
  base.writeUInt8(1, 45);
  if (opts.freezeAuth) {
    base.writeUInt32LE(1, 46);
    Buffer.from(base58Decode(opts.freezeAuth)).copy(base, 50);
  }
  const exts = opts.extensions ?? [];
  if (!opts.token2022 || exts.length === 0) return base.toString('base64');

  const tlv = Buffer.concat(
    exts.map((e) => {
      const b = Buffer.alloc(4);
      b.writeUInt16LE(e, 0);
      b.writeUInt16LE(0, 2);
      return b;
    }),
  );
  const full = Buffer.alloc(166 + tlv.length);
  base.copy(full, 0);
  full.writeUInt8(1, 165); // AccountType::Mint
  tlv.copy(full, 166);
  return full.toString('base64');
}

describe('base58', () => {
  it('round-trips 32 bytes', () => {
    const bytes = new Uint8Array(32).map((_, i) => (i * 37) % 256);
    expect([...base58Decode(base58Encode(bytes))]).toEqual([...bytes]);
  });
});

describe('momentumSizeFactor', () => {
  it('scales size from floor (no inflow) to 1.0 (full inflow)', () => {
    expect(momentumSizeFactor(0, 0.5, 0.4)).toBeCloseTo(0.4, 6); // no momentum -> floor
    expect(momentumSizeFactor(-1, 0.5, 0.4)).toBeCloseTo(0.4, 6); // net outflow clamps to floor
    expect(momentumSizeFactor(0.25, 0.5, 0.4)).toBeCloseTo(0.7, 6); // halfway -> floor + half of (1-floor)
    expect(momentumSizeFactor(0.5, 0.5, 0.4)).toBeCloseTo(1.0, 6); // full inflow -> full size
    expect(momentumSizeFactor(5, 0.5, 0.4)).toBeCloseTo(1.0, 6); // beyond full clamps to 1.0
  });

  it('is a no-op (1.0) when the full-inflow scale is non-positive', () => {
    expect(momentumSizeFactor(0.1, 0, 0.4)).toBe(1);
  });
});

describe('decodeMint', () => {
  it('reads active vs revoked authorities', () => {
    const active = decodeMint(buildMintBase64({ mintAuth: PUBKEY, freezeAuth: null }), PROGRAM_IDS.TOKEN);
    expect(active.mintAuthority).toBe(PUBKEY);
    expect(active.freezeAuthority).toBeNull();
    expect(active.isToken2022).toBe(false);

    const clean = decodeMint(buildMintBase64({ mintAuth: null, freezeAuth: null, decimals: 9 }), PROGRAM_IDS.TOKEN);
    expect(clean.mintAuthority).toBeNull();
    expect(clean.decimals).toBe(9);
  });

  it('parses Token-2022 rug extensions', () => {
    const data = buildMintBase64({
      mintAuth: null,
      freezeAuth: null,
      token2022: true,
      extensions: [MintExtension.TransferHook, MintExtension.PermanentDelegate],
    });
    const info = decodeMint(data, PROGRAM_IDS.TOKEN_2022);
    expect(info.isToken2022).toBe(true);
    expect(info.extensions).toContain(MintExtension.TransferHook);
    expect(info.extensions).toContain(MintExtension.PermanentDelegate);
  });
});

function candidate(mintInfo: Candidate['enrichment']['mintInfo']): Candidate {
  const graduation: GraduationEvent = {
    mint: 'MintUnderTest',
    venue: 'pumpswap',
    poolAddress: '',
    slot: 1,
    feedSource: 'pumpportal',
    receivedAtNs: 0n,
  };
  const enrichment: Candidate['enrichment'] = mintInfo
    ? { unknowns: [], elapsedMs: 5, mintInfo, metadata: { hasSocials: true, name: 'X', symbol: 'X' } }
    : { unknowns: ['mintInfo'], elapsedMs: 5 };
  return { graduation, enrichment };
}

const HEALTHY_MINT = {
  isToken2022: false,
  mintAuthority: null,
  freezeAuthority: null,
  supply: 1000n,
  decimals: 6,
  extensions: [],
};

const CREATOR = base58Encode(Buffer.alloc(32).fill(9));

function healthyPool(over: Partial<PoolInfo> = {}): PoolInfo {
  return {
    poolAddress: 'pool',
    baseMint: 'MintUnderTest',
    quoteMint: 'So11111111111111111111111111111111111111112',
    lpMint: 'lp',
    baseVault: 'baseVault',
    quoteVault: 'quoteVault',
    creator: 'poolCreator',
    coinCreator: CREATOR,
    isCanonical: true,
    baseReserve: 1_000_000_000_000n,
    quoteReserveLamports: 30n * 1_000_000_000n,
    ...over,
  };
}

/** What the fast read hands the engine for a clean canonical graduation. */
function fastCandidate(over: Partial<Candidate['enrichment']> = {}): Candidate {
  const c = candidate(HEALTHY_MINT);
  c.enrichment = { unknowns: [], elapsedMs: 5, mintInfo: HEALTHY_MINT, pool: healthyPool(), ...over };
  return c;
}

function holders(shares: Array<{ share: number; owner?: string }> = []): NonNullable<Candidate['enrichment']['holders']> {
  const rows: Array<{ share: number; owner?: string }> =
    shares.length > 0 ? shares : Array.from({ length: 10 }, () => ({ share: 0.01 }));
  return {
    supply: 1_000_000_000n,
    decimals: 6,
    holders: rows.map((h, i) => ({
      account: `holder${i}`,
      ...(h.owner ? { owner: h.owner } : {}),
      amount: BigInt(Math.round(h.share * 1_000_000_000)),
      share: h.share,
    })),
    top10Share: rows.slice(0, 10).reduce((s, h) => s + h.share, 0),
    maxShare: Math.max(...rows.map((h) => h.share)),
  };
}

function candidateWithFlow(netInflowSol: number, windowMs: number): Candidate {
  const c = candidate(HEALTHY_MINT);
  const endLamports = BigInt(Math.round(netInflowSol * 1e9));
  c.enrichment.earlyFlow = computeEarlyFlow(0n, endLamports, windowMs);
  return c;
}

describe('soft scoring', () => {
  it('maps score to size multiplier per Section 6.2', () => {
    expect(sizeMultiplierFor(59)).toBe(0);
    expect(sizeMultiplierFor(60)).toBeCloseTo(0.5, 5);
    expect(sizeMultiplierFor(80)).toBeCloseTo(1.0, 5);
    expect(sizeMultiplierFor(100)).toBe(1.25);
  });

  it('rewards clean authorities + socials', () => {
    const s = scoreCandidate(candidate(HEALTHY_MINT));
    expect(s.score).toBeGreaterThanOrEqual(60);
  });

  it('gives the clean-mint bonus to Token-2022 tokens with only benign extensions', () => {
    // pump.fun issues Token-2022 mints with metadata extensions (18, 19).
    const benignT22 = { ...HEALTHY_MINT, isToken2022: true, extensions: [18, 19] };
    const rugT22 = { ...HEALTHY_MINT, isToken2022: true, extensions: [12] }; // PermanentDelegate
    expect(scoreCandidate(candidate(benignT22)).score).toBe(scoreCandidate(candidate(HEALTHY_MINT)).score);
    expect(scoreCandidate(candidate(rugT22)).score).toBeLessThan(scoreCandidate(candidate(benignT22)).score);
  });

  it('rewards early net SOL inflow and penalizes net outflow', () => {
    const base = scoreCandidate(candidate(HEALTHY_MINT)).score;
    const inflow = scoreCandidate(candidateWithFlow(15, 4000)).score; // strong buying
    const outflow = scoreCandidate(candidateWithFlow(-15, 4000)).score; // net sells
    expect(inflow).toBeGreaterThan(base);
    expect(outflow).toBeLessThan(base);
    // Bonus is capped at maxScoreBonus (15) either side of the structural baseline.
    expect(inflow - base).toBe(DEFAULT_MOMENTUM_OPTS.maxScoreBonus);
    expect(base - outflow).toBe(DEFAULT_MOMENTUM_OPTS.maxScoreBonus);
  });

  it('flags highVolatility only when the inflow rate is fast (tightens the trail)', () => {
    // No early-flow signal → never high-vol.
    expect(scoreCandidate(candidate(HEALTHY_MINT)).highVolatility).toBe(false);

    // 15 SOL over 4s = 3.75 SOL/s >= 2 SOL/s threshold → high-vol.
    expect(3.75).toBeGreaterThanOrEqual(DEFAULT_MOMENTUM_OPTS.highVolInflowRateSolPerSec);
    expect(scoreCandidate(candidateWithFlow(15, 4000)).highVolatility).toBe(true);

    // 4 SOL over 4s = 1 SOL/s < threshold → still rewarded, but not high-vol.
    const slow = scoreCandidate(candidateWithFlow(4, 4000));
    expect(slow.highVolatility).toBe(false);
    expect(slow.score).toBeGreaterThan(scoreCandidate(candidate(HEALTHY_MINT)).score);
  });
});

describe('GuardrailEngine (fast path)', () => {
  const fresh = () => new Repositories(openDb({ path: ':memory:', memory: true }));
  const cfgs = {
    paper: ConfigSchema.parse({ mode: 'paper' }),
    live: ConfigSchema.parse({ mode: 'live', rpc: { primaryHttp: 'http://x' } }),
  };
  const check = (v: CandidateVerdict, id: string) => v.hardChecks.find((c) => c.id === id);

  for (const [mode, cfg] of Object.entries(cfgs)) {
    it(`${mode}: accepts a clean canonical graduation from the fast read alone — no check comes back unknown`, () => {
      const v = new GuardrailEngine(cfg, fresh()).evaluate(fastCandidate());
      expect(v.verdict).toBe('accept');
      expect(v.hardChecks.map((c) => c.id)).toEqual(['P0', 'H6', 'H7', 'H8', 'H10', 'H12', 'H13']);
      expect(v.hardChecks.every((c) => c.status !== 'unknown')).toBe(true);
      expect(v.relaxedRisk).toBe(false);
    });
  }

  it('no longer runs the removed checks', () => {
    const ids = new GuardrailEngine(cfgs.live, fresh()).evaluate(fastCandidate()).hardChecks.map((c) => c.id);
    for (const gone of ['H1', 'H2', 'H3', 'H4', 'H5', 'H9', 'H11']) expect(ids).not.toContain(gone);
  });

  it('does not gate on the soft score (LOW_SCORE is gone)', () => {
    const c = fastCandidate({ earlyFlow: computeEarlyFlow(0n, -15_000_000_000n, 4000) }); // net sells pull the score down
    const v = new GuardrailEngine(cfgs.live, fresh()).evaluate(c);
    expect(v.softScore).toBeLessThan(60);
    expect(v.verdict).toBe('accept');
  });

  describe('P0 canonical pump.fun migration', () => {
    const p0 = (c: Candidate) => check(new GuardrailEngine(cfgs.paper, fresh()).evaluate(c), 'P0')!;

    it('fails with the fast read reason when there is no pool at the canonical PDA', () => {
      const c = fastCandidate({ unknowns: ['pool:pool_not_found'] });
      delete c.enrichment.pool;
      expect(p0(c)).toMatchObject({ status: 'fail', reason: 'pool_not_found' });
      const v = new GuardrailEngine(cfgs.paper, fresh()).evaluate(c);
      expect(v.verdict).toBe('veto');
    });

    it('fails a pool with no coin_creator (not a pump.fun migration)', () => {
      expect(p0(fastCandidate({ pool: healthyPool({ isCanonical: false }) }))).toMatchObject({ status: 'fail', reason: 'non_canonical' });
    });

    it('fails when the mint did not come back in the same read', () => {
      const c = fastCandidate();
      delete c.enrichment.mintInfo;
      expect(p0(c)).toMatchObject({ status: 'fail', reason: 'mint_unreadable' });
    });

    it('re-checks the authorities and Token-2022 traps from the same read', () => {
      expect(p0(fastCandidate({ mintInfo: { ...HEALTHY_MINT, mintAuthority: PUBKEY } }))).toMatchObject({ status: 'fail', reason: 'mint_authority' });
      expect(p0(fastCandidate({ mintInfo: { ...HEALTHY_MINT, freezeAuthority: PUBKEY } }))).toMatchObject({ status: 'fail', reason: 'freeze_authority' });
      const trap = { ...HEALTHY_MINT, isToken2022: true, extensions: [MintExtension.TransferHook] };
      expect(p0(fastCandidate({ mintInfo: trap }))).toMatchObject({ status: 'fail', reason: 'rug_extension' });
      const benign = { ...HEALTHY_MINT, isToken2022: true, extensions: [18, 19] }; // pump.fun metadata extensions
      expect(p0(fastCandidate({ mintInfo: benign })).status).toBe('pass');
    });
  });

  describe('H6 creator holdings (creator ATA from the fast read)', () => {
    const h6 = (c: Candidate) => check(new GuardrailEngine(cfgs.live, fresh()).evaluate(c), 'H6')!;

    it('does not veto when the creator was not known before graduation', () => {
      expect(h6(fastCandidate())).toMatchObject({ status: 'pass', reason: 'not_checked' });
    });

    it('fails over the cap and passes under it', () => {
      expect(h6(fastCandidate({ creatorHolding: { creator: CREATOR, share: 0.3 } }))).toMatchObject({ status: 'fail' });
      expect(h6(fastCandidate({ creatorHolding: { creator: CREATOR, share: 0.01 } })).status).toBe('pass');
    });
  });

  it('H7 fails a pool under the SOL floor', () => {
    const v = new GuardrailEngine(cfgs.paper, fresh()).evaluate(fastCandidate({ pool: healthyPool({ quoteReserveLamports: 10n * 1_000_000_000n }) }));
    expect(check(v, 'H7')?.status).toBe('fail');
    expect(v.vetoReasons).toEqual(['H7']);
  });

  it('H8 still vetoes a blacklisted mint or creator', () => {
    const repos = fresh();
    repos.blacklistCreator(CREATOR, 'test');
    expect(new GuardrailEngine(cfgs.paper, repos).evaluate(fastCandidate()).vetoReasons).toContain('H8');
  });

  describe('H13 funding cluster (precomputed cache only)', () => {
    const cfg = ConfigSchema.parse({ mode: 'paper', guardrails: { features: { enabled: true }, creatorMaxLaunches7d: 2 } });

    it('does not veto a creator whose cluster was not precomputed', () => {
      expect(check(new GuardrailEngine(cfg, fresh()).evaluate(fastCandidate()), 'H13')).toMatchObject({ status: 'pass', reason: 'not_checked' });
    });

    it('fails a cached cluster that launched more than creatorMaxLaunches7d coins', () => {
      const repos = fresh();
      repos.upsertWalletFunder(CREATOR, 'Funder', 'Root');
      repos.upsertWalletFunder('Sibling', 'Funder', 'Root');
      for (const [i, creator] of [CREATOR, CREATOR, 'Sibling'].entries()) {
        repos.recordLaunch({ mint: `m${i}`, feedSource: 'laserstream', receivedAtNs: 0n, creator });
      }
      const r = check(new GuardrailEngine(cfg, repos).evaluate(fastCandidate()), 'H13');
      expect(r).toMatchObject({ status: 'fail', reason: 'creator_cluster' });
      expect(r?.detail).toContain('launched 3 coins');
    });
  });
});

describe('P2.2 H12 population', () => {
  const PUMP_MINT = 'So1dCanonica1MintAddressXXXXXXXXXXXXXXXpump';
  const cfg = ConfigSchema.parse({ mode: 'dry-run', guardrails: { population: { enabled: true } } });
  const segA = (over: { mint?: string; poolSol?: number; slot?: number; detectedAtMs?: number } = {}) => {
    const c = fastCandidate({ pool: healthyPool({ quoteReserveLamports: BigInt(Math.round((over.poolSol ?? 80) * 1e9)) }) });
    c.graduation = {
      ...c.graduation,
      mint: over.mint ?? PUMP_MINT,
      slot: over.slot ?? 1_000,
      ...(over.detectedAtMs !== undefined ? { detectedAtMs: over.detectedAtMs } : {}),
    };
    return c;
  };
  const h12 = (repos: Repositories, c: Candidate) =>
    new GuardrailEngine(cfg, repos).evaluate(c).hardChecks.find((x) => x.id === 'H12')!;
  const fresh = () => new Repositories(openDb({ path: ':memory:', memory: true }));

  /** Repos with a launch row 10 min (1,500 slots) before a slot-2,500 migration. */
  const launched = (mint = PUMP_MINT) => {
    const repos = fresh();
    repos.recordLaunch({ mint, feedSource: 'pumpportal', receivedAtNs: 0n, slot: 1_000 });
    return repos;
  };

  it('passes a segment-A graduation', () => {
    expect(h12(launched(), segA({ slot: 2_500 })).status).toBe('pass');
  });

  it('fails a non-pump suffix', () => {
    const mint = 'SomeOtherMintAddressWithoutTheSuffixXXXXX';
    const r = h12(launched(mint), segA({ mint, slot: 2_500 }));
    expect(r).toMatchObject({ status: 'fail', reason: 'non_pump_suffix' });
  });

  it('fails pools outside 60–90 SOL', () => {
    expect(h12(launched(), segA({ poolSol: 45, slot: 2_500 }))).toMatchObject({ status: 'fail', reason: 'pool_sol_out_of_band' });
    expect(h12(launched(), segA({ poolSol: 120, slot: 2_500 }))).toMatchObject({ status: 'fail', reason: 'pool_sol_out_of_band' });
  });

  it('fails an insta-graduation measured from the launch slot', () => {
    const repos = fresh();
    repos.recordLaunch({ mint: PUMP_MINT, feedSource: 'pumpportal', receivedAtNs: 0n, slot: 997 });
    // 3 slots = 1.2 s.
    expect(h12(repos, segA({ slot: 1_000 }))).toMatchObject({ status: 'fail', reason: 'insta_graduation' });
  });

  it('fails unknown mint age by default — in dry-run too, where unknowns never veto', () => {
    const v = new GuardrailEngine(cfg, fresh()).evaluate(segA());
    expect(v.hardChecks.find((x) => x.id === 'H12')).toMatchObject({ status: 'fail', reason: 'mint_age_unknown' });
    expect(v.vetoReasons).toContain('H12');
  });

  it('fails closed with no_pool when the pool is missing — vetoed in dry-run', () => {
    const c = segA();
    delete (c.enrichment as unknown as Record<string, unknown>).pool;
    const v = new GuardrailEngine(cfg, fresh()).evaluate(c);
    expect(v.hardChecks.find((x) => x.id === 'H12')).toMatchObject({ status: 'fail', reason: 'no_pool' });
    expect(v.verdict).toBe('veto');
    expect(v.vetoReasons).toContain('H12');
  });

  const withCurve = (c: Candidate, curve: { creationSlot: number | null; oldestSlotScanned: number | null }) => {
    c.enrichment.features = {
      curve: { ...curve, txScanned: 1, bundleSharePct: null, creationSlotBuyers: null, washRatio: null },
    };
    return c;
  };

  it('fails an insta-graduation measured from the curve creation slot', () => {
    const r = h12(fresh(), withCurve(segA({ slot: 1_000 }), { creationSlot: 999, oldestSlotScanned: 999 }));
    expect(r).toMatchObject({ status: 'fail', reason: 'insta_graduation' });
    expect(r.detail).toContain('curve_slot');
  });

  it('passes a mint whose curve creation slot is well before migration', () => {
    const r = h12(fresh(), withCurve(segA({ slot: 2_000 }), { creationSlot: 1_000, oldestSlotScanned: 1_000 }));
    expect(r.status).toBe('pass');
    expect(r.detail).toContain('curve_slot');
  });

  it('prefers the curve creation slot over the launch clock', () => {
    const repos = fresh();
    repos.recordLaunch({ mint: PUMP_MINT, feedSource: 'pumpportal', receivedAtNs: 0n });
    // Launch row inserted "now" -> launch_clock would say ~0 s; the curve says 400 s.
    const c = withCurve(segA({ slot: 2_000, detectedAtMs: Date.now() }), { creationSlot: 1_000, oldestSlotScanned: 1_000 });
    expect(h12(repos, c).status).toBe('pass');
  });

  it('passes a mint created before the process started on a curve lower bound (no launch row)', () => {
    // 2026-09-28 regression: ZYNg…pump / 22NN…pump vetoed as mint_age_unknown.
    const r = h12(fresh(), withCurve(segA({ slot: 2_000 }), { creationSlot: null, oldestSlotScanned: 1_000 }));
    expect(r.status).toBe('pass');
    expect(r.detail).toContain('>= 400 s old (curve_lower_bound)');
  });

  it('treats a short curve lower bound as unknown, not as an insta-graduation', () => {
    const r = h12(fresh(), withCurve(segA({ slot: 1_000 }), { creationSlot: null, oldestSlotScanned: 990 }));
    expect(r).toMatchObject({ status: 'fail', reason: 'mint_age_unknown' });
  });

  it('is a no-op when disabled', () => {
    const off = ConfigSchema.parse({ mode: 'paper' });
    const r = new GuardrailEngine(off, fresh()).evaluate(segA({ mint: 'NoSuffix' })).hardChecks.find((x) => x.id === 'H12');
    expect(r?.status).toBe('pass');
  });
});

describe('H12 launch-stream coverage window', () => {
  const PUMP_MINT = 'So1dCanonica1MintAddressXXXXXXXXXXXXXXXpump';
  const cfg = ConfigSchema.parse({ mode: 'dry-run', guardrails: { population: { enabled: true } } });
  const at = 1_000_000_000;
  const c = () => {
    const x = fastCandidate({ pool: healthyPool({ quoteReserveLamports: 80n * 1_000_000_000n }) });
    x.graduation = { ...x.graduation, mint: PUMP_MINT, slot: 5_000, detectedAtMs: at };
    return x;
  };
  const h12 = (coverageSince: number | null, repos = new Repositories(openDb({ path: ':memory:', memory: true }))) =>
    new GuardrailEngine(cfg, repos, undefined, () => coverageSince).evaluate(c()).hardChecks.find((x) => x.id === 'H12')!;

  it('passes a mint never seen created while the stream covered longer than minMintAgeMs', () => {
    const r = h12(at - 45_000);
    expect(r.status).toBe('pass');
    expect(r.detail).toContain('coverage');
  });

  it('still fails an unknown age when the window is shorter than minMintAgeMs', () => {
    expect(h12(at - 10_000)).toMatchObject({ status: 'fail', reason: 'mint_age_unknown' });
    expect(h12(null)).toMatchObject({ status: 'fail', reason: 'mint_age_unknown' });
  });

  it('lets a seen creation decide: an insta-graduation fails even under full coverage', () => {
    const repos = new Repositories(openDb({ path: ':memory:', memory: true }));
    repos.recordLaunch({ mint: PUMP_MINT, feedSource: 'laserstream', receivedAtNs: 0n, slot: 4_997 });
    expect(h12(at - 3_600_000, repos)).toMatchObject({ status: 'fail', reason: 'insta_graduation' });
  });
});

// ---------------------------------------------------------------------------
// Fast read: one batched processed read → pool, mint, reserves, creator bag
// ---------------------------------------------------------------------------

const POOL_DISCRIMINATOR = Buffer.from('f19a6d0411b16dbc', 'hex');

function poolAccountData(p: { mint: string; coinCreator: string; baseVault: string; quoteVault: string }): string {
  const buf = Buffer.alloc(300);
  POOL_DISCRIMINATOR.copy(buf, 0);
  const put = (off: number, key: string) => Buffer.from(base58Decode(key)).copy(buf, off);
  put(11, PUBKEY);
  put(43, p.mint);
  put(75, WSOL_MINT);
  put(107, PUBKEY);
  put(139, p.baseVault);
  put(171, p.quoteVault);
  put(211, p.coinCreator);
  return buf.toString('base64');
}

function tokenAccountData(amount: bigint): string {
  const buf = Buffer.alloc(165);
  buf.writeBigUInt64LE(amount, 64);
  return buf.toString('base64');
}

/** A chain of one canonical graduation, served through getMultipleAccountsBase64. */
function fakeChain(opts: { quoteLamports?: bigint; creatorTokens?: bigint; poolAppearsOnCall?: number; wrongVaults?: boolean } = {}) {
  const mint = base58Encode(Buffer.alloc(32).fill(3));
  const pool = canonicalPumpPoolPda(new PublicKey(mint)).toBase58();
  const derivedBase = deriveAta(pool, mint, false);
  const derivedQuote = deriveAta(pool, WSOL_MINT, false);
  const baseVault = opts.wrongVaults ? base58Encode(Buffer.alloc(32).fill(5)) : derivedBase;
  const quoteVault = opts.wrongVaults ? base58Encode(Buffer.alloc(32).fill(6)) : derivedQuote;
  const accounts = new Map<string, { data: string; owner: string; lamports: number; executable: boolean }>([
    [pool, { data: poolAccountData({ mint, coinCreator: CREATOR, baseVault, quoteVault }), owner: PROGRAM_IDS.PUMP_SWAP, lamports: 1, executable: false }],
    [mint, { data: buildMintBase64({ supply: 1_000_000_000n }), owner: PROGRAM_IDS.TOKEN, lamports: 1, executable: false }],
    [baseVault, { data: tokenAccountData(800_000_000n), owner: PROGRAM_IDS.TOKEN, lamports: 1, executable: false }],
    [quoteVault, { data: tokenAccountData(opts.quoteLamports ?? 80_000_000_000n), owner: PROGRAM_IDS.TOKEN, lamports: 1, executable: false }],
    [deriveAta(CREATOR, mint, false), { data: tokenAccountData(opts.creatorTokens ?? 50_000_000n), owner: PROGRAM_IDS.TOKEN, lamports: 1, executable: false }],
  ]);
  const calls: string[][] = [];
  const rpc = {
    getMultipleAccountsBase64: async (keys: string[]) => {
      calls.push(keys);
      const poolVisible = calls.length >= (opts.poolAppearsOnCall ?? 1);
      return keys.map((k) => (k === pool && !poolVisible ? null : accounts.get(k) ?? null));
    },
  };
  const graduation: GraduationEvent = { mint, venue: 'pumpswap', poolAddress: '', slot: 10, feedSource: 'laserstream', receivedAtNs: 0n };
  return { mint, pool, rpc, calls, graduation, baseVault, quoteVault };
}

describe('FastPoolReader', () => {
  const noSleep = async () => {};

  it('gets pool, mint, reserves and the creator bag from ONE batched read', async () => {
    const chain = fakeChain();
    const reader = new FastPoolReader({ rpc: chain.rpc, retryDelaysMs: [0], launchCreator: () => CREATOR, sleep: noSleep });
    const r = await reader.read(chain.graduation);
    expect(chain.calls).toHaveLength(1);
    expect(r.ok).toBe(true);
    if (!r.ok) return;
    expect(r.snapshot.pool).toMatchObject({ poolAddress: chain.pool, baseMint: chain.mint, isCanonical: true, quoteReserveLamports: 80_000_000_000n });
    expect(r.snapshot.mintInfo?.mintAuthority).toBeNull();
    expect(r.snapshot.creatorHolding).toEqual({ creator: CREATOR, share: 0.05 });
    expect(r.snapshot.reservesFrom).toBe('rpc');
  });

  it('skips the creator bag when the launch feed named someone other than coin_creator', async () => {
    const chain = fakeChain();
    const reader = new FastPoolReader({ rpc: chain.rpc, retryDelaysMs: [0], launchCreator: () => PUBKEY, sleep: noSleep });
    const r = await reader.read(chain.graduation);
    expect(r.ok && r.snapshot.creatorHolding).toBeUndefined();
  });

  it('retries while the RPC node has not seen the pool yet, then gives up with pool_not_found', async () => {
    const lagging = fakeChain({ poolAppearsOnCall: 3 });
    const r = await new FastPoolReader({ rpc: lagging.rpc, retryDelaysMs: [0, 50, 100, 200], sleep: noSleep }).read(lagging.graduation);
    expect(r.ok).toBe(true);
    expect(lagging.calls).toHaveLength(3);

    const never = fakeChain({ poolAppearsOnCall: 99 });
    const miss = await new FastPoolReader({ rpc: never.rpc, retryDelaysMs: [0, 50], sleep: noSleep }).read(never.graduation);
    expect(miss).toMatchObject({ ok: false, reason: 'pool_not_found', attempts: 2 });
  });

  it("prices a lagging vault read from the migrate tx's own post balances instead of waiting a slot", async () => {
    const chain = fakeChain({ quoteLamports: 0n });
    const slept: number[] = [];
    const g = {
      ...chain.graduation,
      txBalances: [
        { mint: WSOL_MINT, owner: chain.pool, amount: 84_000_000_000n },
        { mint: chain.mint, owner: chain.pool, amount: 206_900_000_000_000n },
        { mint: chain.mint, owner: PUBKEY, amount: 1n },
      ],
    };
    const r = await new FastPoolReader({ rpc: chain.rpc, retryDelaysMs: [0], sleep: async (ms) => void slept.push(ms) }).read(g);
    expect(r.ok && r.snapshot).toMatchObject({ reservesFrom: 'tx', pool: { quoteReserveLamports: 84_000_000_000n, baseReserve: 206_900_000_000_000n } });
    expect(slept).toEqual([]);
    expect(reservesFromTx(g, chain.pool, chain.mint)).toEqual({ base: 206_900_000_000_000n, quote: 84_000_000_000n });
  });

  it('reads the recorded vaults when they are not the derived ATAs', async () => {
    const chain = fakeChain({ wrongVaults: true });
    const r = await new FastPoolReader({ rpc: chain.rpc, retryDelaysMs: [0], sleep: noSleep }).read(chain.graduation);
    expect(chain.calls).toHaveLength(2);
    expect(chain.calls[1]).toEqual([chain.baseVault, chain.quoteVault]);
    expect(r.ok && r.snapshot.pool.quoteReserveLamports).toBe(80_000_000_000n);
  });
});

describe('GuardrailPipeline (fast path)', () => {
  const config = ConfigSchema.parse({ mode: 'paper', guardrails: { backgroundEnrichment: false } });

  it('opens off the single batched read — the verdict follows the open, and nothing else is awaited', async () => {
    const chain = fakeChain();
    const bus = new TypedBus();
    const repos = new Repositories(openDb({ path: ':memory:', memory: true }));
    const order: string[] = [];
    bus.on('openPosition', (e) => order.push(`open:${e.mint}:${e.pricing.quoteReserveLamports}`));
    const verdict = new Promise<CandidateVerdict>((resolve) =>
      bus.on('verdict', (v) => {
        order.push(`verdict:${v.verdict}`);
        resolve(v);
      }),
    );
    const slow = { getMultipleAccountsBase64: () => new Promise(() => {}) } as unknown as RpcClient; // research client never answers
    new GuardrailPipeline({ config, bus, repos, rpc: slow, fastRpc: chain.rpc as unknown as RpcClient }).start();
    bus.emit('graduation', chain.graduation);
    const v = await verdict;
    expect(v.verdict).toBe('accept');
    expect(order).toEqual([`open:${chain.mint}:80000000000`, 'verdict:accept']);
    expect(chain.calls).toHaveLength(1);
  });

  it('persists the verdict after the open, with the hot-path timings', async () => {
    const chain = fakeChain();
    const bus = new TypedBus();
    const repos = new Repositories(openDb({ path: ':memory:', memory: true }));
    new GuardrailPipeline({ config, bus, repos, rpc: chain.rpc as unknown as RpcClient }).start();
    const verdict = new Promise<CandidateVerdict>((resolve) => bus.on('verdict', resolve));
    bus.emit('graduation', chain.graduation);
    await verdict;
    await new Promise((r) => setImmediate(r));
    const row = (repos as unknown as { db: { prepare(s: string): { get(...a: unknown[]): unknown } } }).db
      .prepare('SELECT verdict, features_json, creator_share FROM candidates WHERE mint = ?')
      .get(chain.mint) as { verdict: string; features_json: string };
    expect(row.verdict).toBe('accept');
    expect(JSON.parse(row.features_json).timings).toMatchObject({ fastReadAttempts: 1, reservesFrom: 'rpc' });
  });

  it('vetoes P0 when the canonical pool never shows up', async () => {
    const chain = fakeChain({ poolAppearsOnCall: 99 });
    const bus = new TypedBus();
    const repos = new Repositories(openDb({ path: ':memory:', memory: true }));
    const cfg = ConfigSchema.parse({ mode: 'paper', guardrails: { backgroundEnrichment: false, fastReadRetryDelaysMs: [0] } });
    new GuardrailPipeline({ config: cfg, bus, repos, rpc: chain.rpc as unknown as RpcClient }).start();
    const verdict = new Promise<CandidateVerdict>((resolve) => bus.on('verdict', resolve));
    bus.emit('graduation', chain.graduation);
    const v = await verdict;
    expect(v.verdict).toBe('veto');
    expect(v.hardChecks.find((c) => c.id === 'P0')).toMatchObject({ status: 'fail', reason: 'pool_not_found' });
  });
});

describe('screen replay (offline)', () => {
  const toJson = (v: unknown) => JSON.stringify(v, (_k, x) => (typeof x === 'bigint' ? x.toString() : x));

  it('revives the bigint fields safeJson wrote as strings', () => {
    const c = fastCandidate({ pool: healthyPool({ quoteReserveLamports: 80n * 1_000_000_000n }), holders: holders() });
    const e = reviveEnrichment(toJson(c.enrichment));
    expect(e.pool?.quoteReserveLamports).toBe(80n * 1_000_000_000n);
    expect(e.pool?.baseReserve).toBe(c.enrichment.pool!.baseReserve);
    expect(e.mintInfo?.supply).toBe(HEALTHY_MINT.supply);
    expect(e.holders?.holders[0]?.amount).toBe(c.enrichment.holders!.holders[0]!.amount);
  });

  const row = (c: Candidate, stored: { verdict: 'accept' | 'veto'; primary: string | null; checks: unknown[] }) => ({
    mint: c.graduation.mint,
    enrichmentJson: toJson(c.enrichment),
    hardCheckResults: JSON.stringify(stored.checks),
    verdict: stored.verdict,
    primaryVetoCode: stored.primary,
    slot: 2_000,
    venue: 'pumpswap',
    feedSource: 'helius-ws',
    poolAddress: 'pool',
    detectedAtMs: null,
  });

  it('derives the creator bag from a stored holder snapshot for H6', () => {
    const c = fastCandidate({ holders: holders([{ share: 0.4, owner: CREATOR }, { share: 0.01 }]) });
    expect(candidateFromRow(row(c, { verdict: 'veto', primary: 'H5', checks: [] }))!.enrichment.creatorHolding).toEqual({ creator: CREATOR, share: 0.4 });
  });

  it('reports a stored unknown-driven veto that the fast path accepts', () => {
    const cfg = ConfigSchema.parse({ mode: 'dry-run' });
    const repos = new Repositories(openDb({ path: ':memory:', memory: true }));
    const c = fastCandidate({ holders: holders() });
    const stored = [{ id: 'H11', label: 'x', status: 'fail', reason: 'unindexed_mint' }];
    const r = replayRow(row(c, { verdict: 'veto', primary: 'H11', checks: stored }), cfg, repos)!;
    expect(r.stored).toEqual({ verdict: 'veto', primary: 'H11', reasons: ['H11'] });
    expect(r.replay).toEqual({ verdict: 'accept', primary: null, reasons: [] });
  });
});
