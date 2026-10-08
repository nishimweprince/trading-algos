import { describe, it, expect } from 'vitest';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { PublicKey } from '@solana/web3.js';
import { canonicalPumpPoolPda } from '@pump-fun/pump-swap-sdk';
import { base58Encode, base58Decode } from '../src/core/base58.ts';
import { decodeMint, MintExtension } from '../src/enrichment/mint.ts';
import { PROGRAM_IDS, WSOL_MINT } from '../src/core/constants.ts';
import { deriveAta } from '../src/core/ata.ts';
import { GuardrailEngine } from '../src/guardrails/engine.ts';
import { FastPoolReader, reservesFromTx } from '../src/guardrails/fastRead.ts';
import { PreArmer } from '../src/guardrails/preArm.ts';
import { checkRacerFire, RacerFireFeed } from '../src/guardrails/racerGate.ts';
import { BONDING_CURVE_DISCRIMINATOR, deriveBondingCurvePda } from '../src/enrichment/curve.ts';
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
  return { virtualQuoteLamports: 0n,
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
      expect(v.hardChecks.map((c) => c.id)).toEqual(['P0', 'H7', 'H8', 'H10', 'H12']);
      expect(v.hardChecks.every((c) => c.status !== 'unknown')).toBe(true);
      expect(v.relaxedRisk).toBe(false);
    });
  }

  it('no longer runs the removed checks', () => {
    const ids = new GuardrailEngine(cfgs.live, fresh()).evaluate(fastCandidate()).hardChecks.map((c) => c.id);
    for (const gone of ['H1', 'H2', 'H3', 'H4', 'H5', 'H6', 'H9', 'H11', 'H13']) expect(ids).not.toContain(gone);
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
});

describe('H12 population (pump suffix only, 2026-10-04)', () => {
  const PUMP_MINT = 'So1dCanonica1MintAddressXXXXXXXXXXXXXXXpump';
  const cfg = ConfigSchema.parse({ mode: 'dry-run', guardrails: { population: { enabled: true } } });
  const cand = (over: { mint?: string; poolSol?: number; slot?: number; creatorShare?: number } = {}) => {
    const c = fastCandidate({ pool: healthyPool({ quoteReserveLamports: BigInt(Math.round((over.poolSol ?? 80) * 1e9)) }) });
    c.graduation = { ...c.graduation, mint: over.mint ?? PUMP_MINT, slot: over.slot ?? 1_000 };
    if (over.creatorShare !== undefined) c.enrichment.creatorHolding = { creator: 'dev', share: over.creatorShare };
    return c;
  };
  const engine = () => new GuardrailEngine(cfg, new Repositories(openDb({ path: ':memory:', memory: true })));

  it('fails a non-pump suffix', () => {
    const r = engine().evaluate(cand({ mint: 'So1dCanonica1MintAddressXXXXXXXXXXXXXXXXXXX' })).hardChecks.find((x) => x.id === 'H12');
    expect(r).toMatchObject({ status: 'fail', reason: 'non_pump_suffix' });
  });

  it('accepts what the removed rules used to veto: off-band pool, unknown mint age, heavy creator', () => {
    for (const c of [cand({ poolSol: 45 }), cand({ poolSol: 120 }), cand({ slot: 1 }), cand({ creatorShare: 0.4 })]) {
      const v = engine().evaluate(c);
      expect(v.verdict).toBe('accept');
      expect(v.hardChecks.map((x) => x.id)).toEqual(['P0', 'H7', 'H8', 'H10', 'H12']);
    }
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

  it('gets pool, mint and reserves from ONE batched read — no creator accounts', async () => {
    const chain = fakeChain();
    const reader = new FastPoolReader({ rpc: chain.rpc, retryDelaysMs: [0], sleep: noSleep });
    const r = await reader.read(chain.graduation);
    expect(chain.calls).toHaveLength(1);
    expect(r.ok).toBe(true);
    if (!r.ok) return;
    expect(r.snapshot.pool).toMatchObject({ poolAddress: chain.pool, baseMint: chain.mint, isCanonical: true, quoteReserveLamports: 80_000_000_000n });
    expect(r.snapshot.mintInfo?.mintAuthority).toBeNull();
    expect(r.snapshot.reservesFrom).toBe('rpc');
    expect(chain.calls[0]!.some((k) => k === deriveAta(CREATOR, chain.mint, false))).toBe(false);
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

// ---------------------------------------------------------------------------
// Pre-arm at curve completion: pool pushed + mint prefetched → no RPC read
// ---------------------------------------------------------------------------

describe('PreArmer + FastPoolReader', () => {
  const noSleep = async () => {};
  const cfg = { enabled: true, ttlMs: 60_000, poolWaitMs: 5, maxTrackedLaunches: 1_000 };

  function completion(mint: string, drained = false) {
    const curve = deriveBondingCurvePda(mint)!;
    const data = Buffer.alloc(151);
    Buffer.from(BONDING_CURVE_DISCRIMINATOR, 'hex').copy(data, 0);
    data.writeBigUInt64LE(drained ? 0n : 85_000_000_000n, 32); // realSolReserves
    data[48] = 1;
    Buffer.from(base58Decode(CREATOR)).copy(data, 49);
    return { account: { account: { pubkey: curve, owner: PROGRAM_IDS.PUMP_FUN, data: data.toString('base64'), lamports: '1' } } };
  }

  function migrated(chain: ReturnType<typeof fakeChain>): GraduationEvent {
    return {
      ...chain.graduation,
      txBalances: [
        { mint: WSOL_MINT, owner: chain.pool, amount: 67_405_855_261n },
        { mint: chain.mint, owner: chain.pool, amount: 206_900_000_000_000n },
      ],
    };
  }

  it('serves the verdict read from the armed state with zero RPC on the hot path', async () => {
    const chain = fakeChain();
    const armer = new PreArmer({ endpoint: '', rpc: chain.rpc, config: cfg });
    armer.noteLaunch(chain.mint);
    armer.ingest(completion(chain.mint));
    await new Promise((r) => setTimeout(r, 0)); // prefetch
    const prefetchCalls = chain.calls.length;
    const poolAcct = (await chain.rpc.getMultipleAccountsBase64([chain.pool]))[0]!;
    chain.calls.length = prefetchCalls; // not part of the test's accounting
    armer.ingest({ account: { account: { pubkey: chain.pool, owner: PROGRAM_IDS.PUMP_SWAP, data: poolAcct.data, lamports: 1 } } });

    const reader = new FastPoolReader({ rpc: chain.rpc, retryDelaysMs: [0], preArmer: armer, sleep: noSleep });
    const r = await reader.read(migrated(chain));
    expect(r.ok).toBe(true);
    if (!r.ok) return;
    expect(chain.calls).toHaveLength(prefetchCalls); // nothing after the migration
    expect(r.snapshot.preArmed).toBe(true);
    expect(r.snapshot.reservesFrom).toBe('tx');
    expect(r.snapshot.pool.quoteReserveLamports).toBe(67_405_855_261n);
    expect(armer.stats).toMatchObject({ completions: 1, armed: 1, poolPushes: 1, hits: 1 });
  });

  it('falls back to the batched read when the pool push has not arrived', async () => {
    const chain = fakeChain();
    const armer = new PreArmer({ endpoint: '', rpc: chain.rpc, config: cfg });
    armer.noteLaunch(chain.mint);
    armer.ingest(completion(chain.mint));
    await new Promise((r) => setTimeout(r, 0));
    const reader = new FastPoolReader({ rpc: chain.rpc, retryDelaysMs: [0], preArmer: armer, sleep: noSleep });
    const r = await reader.read(migrated(chain));
    expect(r.ok && r.snapshot.preArmed).toBe(false);
    expect(chain.calls.length).toBe(2); // prefetch + the normal read
  });

  it('race: a pool push that lands while the RPC read is in flight wins', async () => {
    const chain = fakeChain();
    const armer = new PreArmer({ endpoint: '', rpc: chain.rpc, config: cfg });
    armer.noteLaunch(chain.mint);
    armer.ingest(completion(chain.mint));
    await new Promise((r) => setTimeout(r, 0)); // prefetch done
    const poolAcct = (await chain.rpc.getMultipleAccountsBase64([chain.pool]))[0]!;
    const slowRpc = {
      getMultipleAccountsBase64: async (keys: string[]) => {
        await new Promise((r) => setTimeout(r, 200));
        return chain.rpc.getMultipleAccountsBase64(keys);
      },
    };
    const reader = new FastPoolReader({ rpc: slowRpc, retryDelaysMs: [0], preArmer: armer, poolRaceMs: 1_000, sleep: noSleep });
    setTimeout(() => armer.ingest({ account: { account: { pubkey: chain.pool, owner: PROGRAM_IDS.PUMP_SWAP, data: poolAcct.data, lamports: 1 } } }), 20);
    const t0 = Date.now();
    const r = await reader.read(migrated(chain));
    expect(r.ok && r.snapshot.preArmed).toBe(true);
    expect(Date.now() - t0).toBeLessThan(150); // did not wait for the 200 ms read
    expect(armer.stats).toMatchObject({ hits: 1 });
  });

  it('race: the RPC read wins when no push comes, never waiting out the race window', async () => {
    const chain = fakeChain();
    const armer = new PreArmer({ endpoint: '', rpc: chain.rpc, config: cfg });
    armer.noteLaunch(chain.mint);
    armer.ingest(completion(chain.mint));
    await new Promise((r) => setTimeout(r, 0));
    const reader = new FastPoolReader({ rpc: chain.rpc, retryDelaysMs: [0], preArmer: armer, poolRaceMs: 5_000, sleep: noSleep });
    const t0 = Date.now();
    const r = await reader.read(migrated(chain));
    expect(r.ok && r.snapshot.preArmed).toBe(false);
    expect(Date.now() - t0).toBeLessThan(100);
    expect(armer.stats).toMatchObject({ misses: 1 });
  });

  it('does not arm a curve that already migrated (drained reserves)', () => {
    const chain = fakeChain();
    const armer = new PreArmer({ endpoint: '', rpc: chain.rpc, config: cfg });
    armer.noteLaunch(chain.mint);
    armer.ingest(completion(chain.mint, true));
    expect(armer.stats).toMatchObject({ completions: 1, alreadyMigrated: 1, armed: 0 });
  });

  it('ignores completions of curves whose launch it never saw', () => {
    const chain = fakeChain();
    const armer = new PreArmer({ endpoint: '', rpc: chain.rpc, config: cfg });
    armer.ingest(completion(chain.mint));
    expect(armer.stats).toMatchObject({ completions: 1, unknownCurve: 1, armed: 0 });
  });

  it('armMint (racer fire) arms with no launch or completion seen, idempotently', () => {
    const chain = fakeChain();
    const armer = new PreArmer({ endpoint: '', rpc: chain.rpc, config: cfg });
    armer.armMint(chain.mint, 600_000);
    armer.armMint(chain.mint, 600_000);
    expect(armer.stats).toMatchObject({ armed: 1, armedOnFire: 1 });
    expect(armer.peek(chain.mint)?.watchKeys).toEqual(expect.arrayContaining([chain.pool, chain.quoteVault, chain.baseVault]));
  });

  it('prices a pre-armed verdict from pushed vaults, so a same-slot pump stays visible to H7', async () => {
    const chain = fakeChain();
    const armer = new PreArmer({ endpoint: '', rpc: chain.rpc, config: cfg });
    armer.armMint(chain.mint, 600_000);
    await new Promise((r) => setTimeout(r, 0)); // prefetch
    const prefetchCalls = chain.calls.length;
    const [poolAcct, baseAcct] = await chain.rpc.getMultipleAccountsBase64([chain.pool, chain.baseVault]);
    chain.calls.length = prefetchCalls;
    const push = (pubkey: string, owner: string, data: string) => armer.ingest({ account: { account: { pubkey, owner, data, lamports: 1 } } });
    push(chain.pool, PROGRAM_IDS.PUMP_SWAP, poolAcct!.data);
    push(chain.baseVault, PROGRAM_IDS.TOKEN, baseAcct!.data);
    push(chain.quoteVault, PROGRAM_IDS.TOKEN, tokenAccountData(232_000_000_000n)); // pumped after the migrate tx

    const r = await new FastPoolReader({ rpc: chain.rpc, retryDelaysMs: [0], preArmer: armer, sleep: noSleep }).read(migrated(chain));
    expect(r.ok).toBe(true);
    if (!r.ok) return;
    expect(chain.calls).toHaveLength(prefetchCalls); // still zero RPC on the hot path
    expect(r.snapshot.preArmed).toBe(true);
    expect(r.snapshot.reservesFrom).toBe('push');
    expect(r.snapshot.pool.quoteReserveLamports).toBe(232_000_000_000n); // not the tx's 67.4 SOL
  });
});

describe('racer gate (R1)', () => {
  const fresh = () => new Repositories(openDb({ path: ':memory:', memory: true }));
  const live = ConfigSchema.parse({ mode: 'live', rpc: { primaryHttp: 'http://x' } });

  it('vetoes a migration the racer never fired on and accepts one it did', () => {
    const repos = fresh();
    const engine = new GuardrailEngine(live, repos, undefined, [checkRacerFire]);
    const c = fastCandidate();
    const before = engine.evaluate(c);
    expect(before.verdict).toBe('veto');
    expect(before.vetoReasons).toEqual(['R1']);

    repos.upsertRacerCurve({ mint: c.graduation.mint, curve: null, admitSlot: 1, fireSlot: 7 });
    const after = engine.evaluate(c);
    expect(after.verdict).toBe('accept');
    expect(after.hardChecks.at(-1)).toMatchObject({ id: 'R1', status: 'pass' });
  });

  it('is not part of the shared check set (the racer screen stays ungated)', () => {
    const ids = new GuardrailEngine(live, fresh()).evaluate(fastCandidate()).hardChecks.map((c) => c.id);
    expect(ids).not.toContain('R1');
  });

  it('fire feed: one callback per newly fired open curve', () => {
    let open = ['A', 'B'];
    const fired: string[] = [];
    const feed = new RacerFireFeed({ repos: { openRacerFires: () => open }, pollMs: 100, onFire: (m) => fired.push(m) });
    feed.poll();
    open = ['B', 'C'];
    feed.poll();
    feed.poll();
    expect(fired).toEqual(['A', 'B', 'C']);
  });

  it('reads fires written by another connection to the same DB file', () => {
    const dir = mkdtempSync(join(tmpdir(), 'racer-gate-'));
    const path = join(dir, 'g.db');
    const racer = new Repositories(openDb({ path }));
    const trader = new Repositories(openDb({ path }));
    expect(trader.racerFireSlot('M')).toBeNull();
    racer.upsertRacerCurve({ mint: 'M', curve: null, admitSlot: 1, fireSlot: 9 });
    expect(trader.racerFireSlot('M')).toBe(9);
    expect(trader.openRacerFires()).toEqual(['M']);
    racer.upsertRacerCurve({ mint: 'M', curve: null, outcome: 'migrated' });
    expect(trader.openRacerFires()).toEqual([]);
    expect(trader.racerFireSlot('M')).toBe(9);
  });
});
