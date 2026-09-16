import { describe, it, expect, vi } from 'vitest';
import { PublicKey } from '@solana/web3.js';
import { canonicalPumpPoolPda } from '@pump-fun/pump-swap-sdk';
import { base58Encode, base58Decode } from '../src/core/base58.ts';
import { decodePool, decodeTokenAccountAmount, fetchPumpSwapPool, type PoolInfo } from '../src/enrichment/pool.ts';
import { PROGRAM_IDS, WSOL_MINT, LAMPORTS_PER_SOL } from '../src/core/constants.ts';
import { checkLpStatus, checkHolderConcentration, checkCreatorHoldings, checkLiquidityFloor } from '../src/guardrails/checks/pool.ts';
import { ConfigSchema } from '../src/config/schema.ts';
import { openDb } from '../src/persistence/db.ts';
import { Repositories } from '../src/persistence/repositories.ts';
import type { RpcClient } from '../src/core/rpc.ts';
import type { Candidate, HolderInfo } from '../src/enrichment/types.ts';
import type { GraduationEvent } from '../src/core/types.ts';
import type { CheckContext } from '../src/guardrails/engine.ts';

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

describe('fetchPumpSwapPool', () => {
  const mint = pk(1);
  const canonicalAddress = canonicalPumpPoolPda(new PublicKey(mint)).toBase58();
  const poolFields = { baseMint: mint, lpMint: pk(3), baseVault: pk(4), quoteVault: pk(5), creator: pk(6), coinCreator: pk(7) };

  function vaultAccount(amount: bigint) {
    const buf = Buffer.alloc(72);
    buf.writeBigUInt64LE(amount, 64);
    return { data: buf.toString('base64'), owner: PROGRAM_IDS.TOKEN, lamports: 0, executable: false };
  }

  function fakeRpc(overrides: Partial<RpcClient> = {}): RpcClient {
    return {
      getAccountInfoBase64: async () => null,
      getProgramAccountsBase64: async () => [],
      getMultipleAccountsBase64: async () => [vaultAccount(1000n), vaultAccount(2000n)],
      getTokenSupply: async () => ({ amount: 0n, decimals: 6 }),
      ...overrides,
    } as unknown as RpcClient;
  }

  it('uses the canonical PDA directly — never calls getProgramAccounts when it resolves', async () => {
    const search = vi.fn(async () => []);
    const rpc = fakeRpc({
      getAccountInfoBase64: async (addr: string) =>
        addr === canonicalAddress
          ? { data: buildPoolBase64(poolFields), owner: PROGRAM_IDS.PUMP_SWAP, lamports: 0 }
          : null,
      getProgramAccountsBase64: search,
    });
    const pool = await fetchPumpSwapPool(rpc, mint);
    expect(pool?.baseMint).toBe(mint);
    expect(pool?.poolAddress).toBe(canonicalAddress);
    expect(search).not.toHaveBeenCalled();
  });

  it('falls back to the memcmp search when the canonical address holds nothing', async () => {
    const rpc = fakeRpc({
      getAccountInfoBase64: async () => null,
      getProgramAccountsBase64: async () => [
        { pubkey: 'NonCanonicalPool', data: buildPoolBase64(poolFields), owner: PROGRAM_IDS.PUMP_SWAP },
      ],
    });
    const pool = await fetchPumpSwapPool(rpc, mint);
    expect(pool?.baseMint).toBe(mint);
    expect(pool?.poolAddress).toBe('NonCanonicalPool');
  });

  it('falls back to the search when the canonical address decodes to a different mint', async () => {
    const otherMint = pk(9);
    const rpc = fakeRpc({
      getAccountInfoBase64: async (addr: string) =>
        addr === canonicalAddress
          ? { data: buildPoolBase64({ ...poolFields, baseMint: otherMint }), owner: PROGRAM_IDS.PUMP_SWAP, lamports: 0 }
          : null,
      getProgramAccountsBase64: async () => [
        { pubkey: 'RealPool', data: buildPoolBase64(poolFields), owner: PROGRAM_IDS.PUMP_SWAP },
      ],
    });
    const pool = await fetchPumpSwapPool(rpc, mint);
    expect(pool?.poolAddress).toBe('RealPool');
  });

  it('returns null when neither path finds the pool', async () => {
    const pool = await fetchPumpSwapPool(fakeRpc(), mint);
    expect(pool).toBeNull();
  });
});

// --- pool-backed checks ---

const repos = new Repositories(openDb({ path: ':memory:', memory: true }));
const cfg = ConfigSchema.parse({ mode: 'paper' });

function ctxWith(pool: PoolInfo | undefined, holders?: HolderInfo[]): CheckContext {
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
  return { candidate, config: cfg, repos, mode: 'paper', walletSol: 0 };
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
    expect(checkLiquidityFloor(ctxWith(POOL)).status).toBe('pass'); // 50 SOL, dust-size impact
  });
  it('fails below the SOL floor', () => {
    expect(checkLiquidityFloor(ctxWith({ ...POOL, quoteReserveLamports: BigInt(10) * BigInt(LAMPORTS_PER_SOL) })).status).toBe('fail');
  });
});

describe('H5 holder concentration', () => {
  const vaultHolder: HolderInfo = { account: 'BASEVAULT', owner: 'POOLPDA', amount: 90n, share: 0.9 };

  it('returns pass or fail when pool and holders are present — never holders unavailable', () => {
    const spread = [
      vaultHolder,
      { account: 'w1', owner: 'W1', amount: 5n, share: 0.05 },
      { account: 'w2', owner: 'W2', amount: 4n, share: 0.04 },
    ];
    const h5 = checkHolderConcentration(ctxWith(POOL, spread));
    expect(h5.status === 'pass' || h5.status === 'fail').toBe(true);
    expect(h5.detail).not.toBe('holders unavailable');
    const h6 = checkCreatorHoldings(ctxWith(POOL, spread));
    expect(h6.status === 'pass' || h6.status === 'fail').toBe(true);
    expect(h6.detail).not.toBe('holders unavailable');
  });

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
