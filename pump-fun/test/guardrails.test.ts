import { describe, it, expect } from 'vitest';
import { base58Encode, base58Decode } from '../src/core/base58.ts';
import { decodeMint, MintExtension } from '../src/enrichment/mint.ts';
import { PROGRAM_IDS } from '../src/core/constants.ts';
import { GuardrailEngine } from '../src/guardrails/engine.ts';
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
    lpMintSupply: 0n,
    ...over,
  };
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

function liveReadyCandidate(over: {
  mintInfo?: Candidate['enrichment']['mintInfo'];
  pool?: Candidate['enrichment']['pool'];
  holders?: NonNullable<Candidate['enrichment']['holders']>;
  sellable?: Candidate['enrichment']['sellable'];
} = {}): Candidate {
  const c = candidate(HEALTHY_MINT);
  const enrichment: Candidate['enrichment'] = {
    unknowns: [],
    elapsedMs: 5,
    mintInfo: HEALTHY_MINT,
    metadata: { hasSocials: true, name: 'X', symbol: 'X' },
    pool: healthyPool(),
    holders: holders(),
    sellable: { status: 'pass', detail: 'ok' },
  };
  if (over.mintInfo) enrichment.mintInfo = over.mintInfo;
  if (over.pool) enrichment.pool = over.pool;
  if (over.holders) enrichment.holders = over.holders;
  if (over.sellable) enrichment.sellable = over.sellable;
  c.enrichment = enrichment;
  return c;
}

describe('GuardrailEngine', () => {
  const repos = new Repositories(openDb({ path: ':memory:', memory: true }));
  const paperCfg = ConfigSchema.parse({ mode: 'paper' });
  const liveCfg = ConfigSchema.parse({
    mode: 'live',
    rpc: { primaryHttp: 'http://x' },
    jito: { blockEngineUrl: 'http://y' },
  });

  it('accepts a healthy mint in paper mode (unknowns do not veto)', () => {
    const engine = new GuardrailEngine(paperCfg, repos);
    const v = engine.evaluate(candidate(HEALTHY_MINT));
    expect(v.verdict).toBe('accept');
    expect(v.hardChecks.find((c) => c.id === 'H1')?.status).toBe('pass');
    expect(v.sizeMultiplier).toBeGreaterThan(0);
  });

  it('vetoes the same mint in live mode (unknowns == fail)', () => {
    const engine = new GuardrailEngine(liveCfg, repos);
    const v = engine.evaluate(candidate(HEALTHY_MINT));
    expect(v.verdict).toBe('veto');
    expect(v.vetoReasons.some((r) => r.startsWith('UNKNOWN:'))).toBe(true);
    expect(v.sizeMultiplier).toBe(0);
  });

  it('vetoes an active mint authority in any mode', () => {
    const engine = new GuardrailEngine(paperCfg, repos);
    const v = engine.evaluate(candidate({ ...HEALTHY_MINT, mintAuthority: PUBKEY }));
    expect(v.verdict).toBe('veto');
    expect(v.vetoReasons).toContain('H1');
  });

  it('vetoes a mint on the blacklist (H8)', () => {
    repos.blacklistMint('MintUnderTest', 'test');
    const engine = new GuardrailEngine(paperCfg, repos);
    const v = engine.evaluate(candidate(HEALTHY_MINT));
    expect(v.vetoReasons).toContain('H8');
  });

  it('admits tx-too-large H4 only when every other check passes and caps it as relaxed risk', () => {
    const cfg = ConfigSchema.parse({
      mode: 'live',
      rpc: { primaryHttp: 'http://x' },
      guardrails: { tolerateTxTooLargeSellability: true },
    });
    const repos = new Repositories(openDb({ path: ':memory:', memory: true }));
    const engine = new GuardrailEngine(cfg, repos);
    const v = engine.evaluate(liveReadyCandidate({
      sellable: { status: 'unknown', reason: 'tx_too_large', detail: 'VersionedTransaction too large' },
    }));

    expect(v.verdict).toBe('accept');
    expect(v.vetoReasons).not.toContain('UNKNOWN:H4');
    expect(v.relaxedRisk).toBe(true);
    expect(v.relaxedReasons).toContain('relaxed_unknown_h4');
    expect(0.03 * v.sizeMultiplier).toBeLessThanOrEqual(0.02);
  });

  it('admits account-setup H4 unknowns only behind the dedicated flag', () => {
    const cfg = ConfigSchema.parse({
      mode: 'live',
      rpc: { primaryHttp: 'http://x' },
      guardrails: { tolerateInconclusiveSellability: true },
    });
    const repos = new Repositories(openDb({ path: ':memory:', memory: true }));
    const v = new GuardrailEngine(cfg, repos).evaluate(liveReadyCandidate({
      sellable: { status: 'unknown', reason: 'account_setup_unavailable', detail: 'AccountNotFound' },
    }));
    expect(v.verdict).toBe('accept');
    expect(v.relaxedReasons).toEqual(['relaxed_unknown_h4']);
  });

  it('keeps wallet, RPC, not-run, and true H4 failures fatal', () => {
    const cfg = ConfigSchema.parse({
      mode: 'live',
      rpc: { primaryHttp: 'http://x' },
      guardrails: { tolerateTxTooLargeSellability: true },
    });
    const repos = new Repositories(openDb({ path: ':memory:', memory: true }));
    const engine = new GuardrailEngine(cfg, repos);

    for (const reason of ['wallet_unfunded', 'rpc_unavailable', 'not_run'] as const) {
      expect(engine.evaluate(liveReadyCandidate({
        sellable: { status: 'unknown', reason, detail: reason },
      })).vetoReasons).toContain('UNKNOWN:H4');
    }
    expect(engine.evaluate(liveReadyCandidate({
      sellable: { status: 'fail', reason: 'sell_failed', detail: 'sell leg failed' },
    })).vetoReasons).toContain('H4');
  });

  it('does not let the H4 lane rescue another hard check or a low score', () => {
    const cfg = ConfigSchema.parse({
      mode: 'live', rpc: { primaryHttp: 'http://x' },
      guardrails: { tolerateInconclusiveSellability: true },
    });
    const repos = new Repositories(openDb({ path: ':memory:', memory: true }));
    const sellable = { status: 'unknown' as const, reason: 'account_setup_unavailable' as const, detail: 'AccountNotFound' };
    const unsafe = new GuardrailEngine(cfg, repos).evaluate(liveReadyCandidate({
      mintInfo: { ...HEALTHY_MINT, freezeAuthority: PUBKEY }, sellable,
    }));
    expect(unsafe.vetoReasons).toContain('H2');
    expect(unsafe.vetoReasons).toContain('UNKNOWN:H4');

    const highScoreGate = ConfigSchema.parse({
      mode: 'live', rpc: { primaryHttp: 'http://x' }, entry: { minEntryScore: 100 },
      guardrails: { tolerateInconclusiveSellability: true },
    });
    expect(new GuardrailEngine(highScoreGate, repos).evaluate(liveReadyCandidate({ sellable })).vetoReasons)
      .toContain('LOW_SCORE');
  });

  it('does not tolerate tx-too-large H4 unknowns when H9 is not clean or the flag is off', () => {
    const off = ConfigSchema.parse({ mode: 'live', rpc: { primaryHttp: 'http://x' } });
    const on = ConfigSchema.parse({
      mode: 'live',
      rpc: { primaryHttp: 'http://x' },
      guardrails: { tolerateTxTooLargeSellability: true },
    });
    const repos = new Repositories(openDb({ path: ':memory:', memory: true }));
    const sellable = { status: 'unknown' as const, reason: 'tx_too_large' as const, detail: 'VersionedTransaction too large' };

    expect(new GuardrailEngine(off, repos).evaluate(liveReadyCandidate({ sellable })).vetoReasons).toContain('UNKNOWN:H4');
    expect(new GuardrailEngine(on, repos).evaluate(liveReadyCandidate({
      mintInfo: { ...HEALTHY_MINT, isToken2022: true, extensions: [12] },
      sellable,
    })).vetoReasons).toContain('H9');
  });

  it('admits the buy-only H4 backstop only behind its flag, as a size-capped relaxed accept', () => {
    const repos = new Repositories(openDb({ path: ':memory:', memory: true }));
    const sellable = {
      status: 'unknown' as const,
      reason: 'buy_only_ok' as const,
      detail: 'atomic probe too large; buy leg simulated cleanly',
    };

    // Flag off: the backstop reason is not tolerated and vetoes like any unknown.
    const off = ConfigSchema.parse({ mode: 'live', rpc: { primaryHttp: 'http://x' } });
    expect(new GuardrailEngine(off, repos).evaluate(liveReadyCandidate({ sellable })).vetoReasons)
      .toContain('UNKNOWN:H4');

    // Flag on: admitted, tagged relaxed-risk, and capped to the relaxed size.
    const on = ConfigSchema.parse({
      mode: 'live',
      rpc: { primaryHttp: 'http://x' },
      guardrails: { sellabilityBuyOnlyBackstop: true },
    });
    const v = new GuardrailEngine(on, repos).evaluate(liveReadyCandidate({ sellable }));
    expect(v.verdict).toBe('accept');
    expect(v.vetoReasons).not.toContain('UNKNOWN:H4');
    expect(v.relaxedRisk).toBe(true);
    expect(v.relaxedReasons).toContain('relaxed_unknown_h4');
    expect(0.03 * v.sizeMultiplier).toBeLessThanOrEqual(0.02);
  });

  it('keeps the buy-only backstop subordinate to H2 (freeze) and H9 (token-2022 traps)', () => {
    const repos = new Repositories(openDb({ path: ':memory:', memory: true }));
    const cfg = ConfigSchema.parse({
      mode: 'live',
      rpc: { primaryHttp: 'http://x' },
      guardrails: { sellabilityBuyOnlyBackstop: true },
    });
    const sellable = {
      status: 'unknown' as const,
      reason: 'buy_only_ok' as const,
      detail: 'buy leg clean',
    };
    // An unrevoked freeze authority (H2) is the classic sell-block honeypot the
    // buy-only leg cannot see — it must still veto and drag H4 down with it.
    const frozen = new GuardrailEngine(cfg, repos).evaluate(liveReadyCandidate({
      mintInfo: { ...HEALTHY_MINT, freezeAuthority: PUBKEY }, sellable,
    }));
    expect(frozen.vetoReasons).toContain('H2');
    expect(frozen.vetoReasons).toContain('UNKNOWN:H4');
    // A Token-2022 trap (H9) likewise vetoes.
    const trapped = new GuardrailEngine(cfg, repos).evaluate(liveReadyCandidate({
      mintInfo: { ...HEALTHY_MINT, isToken2022: true, extensions: [12] }, sellable,
    }));
    expect(trapped.vetoReasons).toContain('H9');
    expect(trapped.vetoReasons).toContain('UNKNOWN:H4');
  });

  it('tags relaxed threshold accepts, caps their size, and rejects multi-relax candidates', () => {
    const cfg = ConfigSchema.parse({
      mode: 'live',
      rpc: { primaryHttp: 'http://x' },
      guardrails: {
        minPoolSol: 15,
        top10HolderCapPct: 35,
        creatorHoldingsCapPct: 8,
        relaxedRiskMaxReasons: 1,
        relaxedRiskSizeMultiplierCap: 0.5,
        relaxedRiskMaxSizeSol: 0.02,
      },
      entry: { baseSizeSol: 0.03, maxSizeSol: 0.04 },
    });
    const repos = new Repositories(openDb({ path: ':memory:', memory: true }));
    const engine = new GuardrailEngine(cfg, repos);

    const h7 = engine.evaluate(liveReadyCandidate({
      pool: healthyPool({ quoteReserveLamports: 20n * 1_000_000_000n }),
    }));
    expect(h7.verdict).toBe('accept');
    expect(h7.relaxedRisk).toBe(true);
    expect(h7.relaxedReasons).toEqual(['relaxed_h7_pool_sol']);
    expect(h7.sizeMultiplier).toBeCloseTo(0.5);

    const multi = engine.evaluate(liveReadyCandidate({
      pool: healthyPool({ quoteReserveLamports: 20n * 1_000_000_000n }),
      holders: holders(Array.from({ length: 10 }, () => ({ share: 0.03 }))),
    }));
    expect(multi.verdict).toBe('veto');
    expect(multi.vetoReasons).toContain('MULTI_RELAXED_RISK');
  });
});

/** Attach an early-flow signal to a healthy candidate: net SOL over a window. */
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
