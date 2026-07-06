import { describe, it, expect } from 'vitest';
import { base58Encode, base58Decode } from '../src/core/base58.ts';
import { decodePool, decodeTokenAccountAmount, type PoolInfo } from '../src/enrichment/pool.ts';
import { PROGRAM_IDS, WSOL_MINT, LAMPORTS_PER_SOL } from '../src/core/constants.ts';
import { checkLpStatus, checkHolderConcentration, checkCreatorHoldings, checkLiquidityFloor } from '../src/guardrails/checks/pool.ts';
import { ConfigSchema } from '../src/config/schema.ts';
import { openDb } from '../src/persistence/db.ts';
import { Repositories } from '../src/persistence/repositories.ts';
import type { Candidate, HolderInfo } from '../src/enrichment/types.ts';
import type { GraduationEvent } from '../src/core/types.ts';

const pk = (seed: number): string => base58Encode(Buffer.alloc(32).fill(seed));

function buildPoolBase64(fields: {
  baseMint: string;
  quoteMint?: string;
  lpMint: string;
  baseVault: string;
  quoteVault: string;
  creator: string;
  coinCreator: string;
  badDisc?: boolean;
}): string {
  const buf = Buffer.alloc(243);
  Buffer.from(fields.badDisc ? '0000000000000000' : 'f19a6d0411b16dbc', 'hex').copy(buf, 0);
  const put = (addr: string, off: number) => Buffer.from(base58Decode(addr)).copy(buf, off);
  put(fields.creator, 11);
  put(fields.baseMint, 43);
  put(fields.quoteMint ?? WSOL_MINT, 75);
  put(fields.lpMint, 107);
  put(fields.baseVault, 139);
  put(fields.quoteVault, 171);
  put(fields.coinCreator, 211);
  return buf.toString('base64');
}

describe('decodePool', () => {
  const base = { baseMint: pk(1), lpMint: pk(3), baseVault: pk(4), quoteVault: pk(5), creator: pk(6), coinCreator: pk(7) };

  it('decodes a valid PumpSwap pool', () => {
    const d = decodePool(buildPoolBase64(base), PROGRAM_IDS.PUMP_SWAP, 'POOL');
    expect(d).not.toBeNull();
    expect(d!.baseMint).toBe(pk(1));
    expect(d!.quoteMint).toBe(WSOL_MINT);
    expect(d!.coinCreator).toBe(pk(7));
  });

  it('rejects wrong program owner', () => {
    expect(decodePool(buildPoolBase64(base), PROGRAM_IDS.TOKEN, 'POOL')).toBeNull();
  });

  it('rejects wrong discriminator', () => {
    expect(decodePool(buildPoolBase64({ ...base, badDisc: true }), PROGRAM_IDS.PUMP_SWAP, 'POOL')).toBeNull();
  });

  it('rejects non-WSOL quote mint', () => {
    expect(decodePool(buildPoolBase64({ ...base, quoteMint: pk(9) }), PROGRAM_IDS.PUMP_SWAP, 'POOL')).toBeNull();
  });
});

describe('decodeTokenAccountAmount', () => {
  it('reads the u64 amount at offset 64', () => {
    const buf = Buffer.alloc(72);
    buf.writeBigUInt64LE(123456789n, 64);
    expect(decodeTokenAccountAmount(buf.toString('base64'))).toBe(123456789n);
  });
});

// --- pool-backed checks ---

const repos = new Repositories(openDb({ path: ':memory:', memory: true }));
const cfg = ConfigSchema.parse({ mode: 'paper' });

function ctxWith(pool: PoolInfo | undefined, holders?: HolderInfo[]): { candidate: Candidate; config: typeof cfg; repos: typeof repos; mode: 'paper' } {
  const graduation: GraduationEvent = { mint: 'M', venue: 'pumpswap', poolAddress: '', slot: 1, feedSource: 'pumpportal', receivedAtNs: 0n };
  const candidate: Candidate = {
    graduation,
    enrichment: {
      unknowns: [],
      elapsedMs: 1,
      ...(pool ? { pool } : {}),
      ...(holders ? { holders: { supply: 100n, decimals: 6, holders, top10Share: 0, maxShare: 0 } } : {}),
    },
  };
  return { candidate, config: cfg, repos, mode: 'paper' };
}

const POOL: PoolInfo = {
  poolAddress: 'POOL', baseMint: 'M', quoteMint: WSOL_MINT, lpMint: 'LP',
  baseVault: 'BASEVAULT', quoteVault: 'QUOTEVAULT', creator: 'C', coinCreator: 'DEV',
  isCanonical: true, baseReserve: 1000n, quoteReserveLamports: BigInt(50) * BigInt(LAMPORTS_PER_SOL), lpMintSupply: 0n,
};

describe('H3 LP status', () => {
  it('passes when lp_mint supply is 0 (burned)', () => {
    expect(checkLpStatus(ctxWith(POOL)).status).toBe('pass');
  });
  it('fails when lp supply is non-zero (withdrawable)', () => {
    expect(checkLpStatus(ctxWith({ ...POOL, lpMintSupply: 5n })).status).toBe('fail');
  });
  it('unknown without a pool', () => {
    expect(checkLpStatus(ctxWith(undefined)).status).toBe('unknown');
  });
});

describe('H7 liquidity floor + impact', () => {
  it('passes a deep pool with low impact', () => {
    expect(checkLiquidityFloor(ctxWith(POOL)).status).toBe('pass'); // 50 SOL, 0.25/50 = 0.5%
  });
  it('fails below the SOL floor', () => {
    expect(checkLiquidityFloor(ctxWith({ ...POOL, quoteReserveLamports: BigInt(10) * BigInt(LAMPORTS_PER_SOL) })).status).toBe('fail');
  });
});

describe('H5 holder concentration', () => {
  const vaultHolder: HolderInfo = { account: 'BASEVAULT', owner: 'POOLPDA', amount: 90n, share: 0.9 };
  it('excludes the pool vault and passes a spread book', () => {
    const holders = [vaultHolder, { account: 'w1', owner: 'W1', amount: 5n, share: 0.05 }, { account: 'w2', owner: 'W2', amount: 4n, share: 0.04 }];
    expect(checkHolderConcentration(ctxWith(POOL, holders)).status).toBe('pass');
  });
  it('fails on a concentrated single non-pool holder', () => {
    const holders = [vaultHolder, { account: 'w1', owner: 'W1', amount: 30n, share: 0.3 }];
    expect(checkHolderConcentration(ctxWith(POOL, holders)).status).toBe('fail');
  });
});

describe('H6 creator holdings', () => {
  it('fails when the dev holds over the cap', () => {
    const holders = [{ account: 'd', owner: 'DEV', amount: 10n, share: 0.1 }];
    expect(checkCreatorHoldings(ctxWith(POOL, holders)).status).toBe('fail'); // 10% > 5%
  });
  it('passes when the dev holds little', () => {
    const holders = [{ account: 'd', owner: 'DEV', amount: 2n, share: 0.02 }];
    expect(checkCreatorHoldings(ctxWith(POOL, holders)).status).toBe('pass');
  });
});
