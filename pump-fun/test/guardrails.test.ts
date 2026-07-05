import { describe, it, expect } from 'vitest';
import { base58Encode, base58Decode } from '../src/core/base58.ts';
import { decodeMint, MintExtension } from '../src/enrichment/mint.ts';
import { PROGRAM_IDS } from '../src/core/constants.ts';
import { GuardrailEngine } from '../src/guardrails/engine.ts';
import { scoreCandidate, sizeMultiplierFor } from '../src/guardrails/scoring.ts';
import { ConfigSchema } from '../src/config/schema.ts';
import { openDb } from '../src/persistence/db.ts';
import { Repositories } from '../src/persistence/repositories.ts';
import type { Candidate } from '../src/enrichment/types.ts';
import type { GraduationEvent } from '../src/core/types.ts';

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
});

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
});
