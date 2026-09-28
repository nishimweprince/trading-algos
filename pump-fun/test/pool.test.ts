import { describe, it, expect, vi } from 'vitest';
import { PublicKey } from '@solana/web3.js';
import { canonicalPumpPoolPda } from '@pump-fun/pump-swap-sdk';
import { base58Encode, base58Decode } from '../src/core/base58.ts';
import { decodePool, decodeTokenAccountAmount, fetchPumpSwapPool, type PoolInfo } from '../src/enrichment/pool.ts';
import { PROGRAM_IDS, WSOL_MINT, LAMPORTS_PER_SOL } from '../src/core/constants.ts';
import { checkCreatorHoldings, checkLiquidityFloor } from '../src/guardrails/checks/pool.ts';
import { ConfigSchema } from '../src/config/schema.ts';
import { openDb } from '../src/persistence/db.ts';
import { Repositories } from '../src/persistence/repositories.ts';
import type { RpcClient } from '../src/core/rpc.ts';
import type { Candidate } from '../src/enrichment/types.ts';
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
      getMultipleAccountsBase64: async () => [vaultAccount(1000n), vaultAccount(85n * 1_000_000_000n)],
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

  describe('re-read under 1 SOL (liquidity not landed yet)', () => {
    const canonical = { getAccountInfoBase64: async (addr: string) => (addr === canonicalAddress ? { data: buildPoolBase64(poolFields), owner: PROGRAM_IDS.PUMP_SWAP, lamports: 0 } : null) };
    const sequence = (...quotes: bigint[]) => {
      let i = 0;
      return vi.fn(async () => [vaultAccount(1000n), vaultAccount(quotes[Math.min(i++, quotes.length - 1)]!)]);
    };
    const sleep = vi.fn(async () => {});

    it('reads the vaults once more after a slot when the quote vault is under 1 SOL', async () => {
      const reads = sequence(0n, 85n * 1_000_000_000n);
      const pool = await fetchPumpSwapPool(fakeRpc({ ...canonical, getMultipleAccountsBase64: reads }), mint, { sleep });
      expect(reads).toHaveBeenCalledTimes(2);
      expect(sleep).toHaveBeenCalledWith(400);
      expect(pool?.quoteReserveLamports).toBe(85n * 1_000_000_000n);
      expect(pool?.reread).toBe(true);
    });

    it('reads once when the pool already holds liquidity', async () => {
      const reads = sequence(80n * 1_000_000_000n);
      const pool = await fetchPumpSwapPool(fakeRpc({ ...canonical, getMultipleAccountsBase64: reads }), mint, { sleep: vi.fn(async () => {}) });
      expect(reads).toHaveBeenCalledTimes(1);
      expect(pool?.reread).toBeUndefined();
    });

    it('re-reads exactly once — a pool still empty after the re-read is reported empty', async () => {
      const reads = sequence(0n, 100_000n, 85n * 1_000_000_000n);
      const pool = await fetchPumpSwapPool(fakeRpc({ ...canonical, getMultipleAccountsBase64: reads }), mint, { sleep: vi.fn(async () => {}) });
      expect(reads).toHaveBeenCalledTimes(2);
      expect(pool?.quoteReserveLamports).toBe(100_000n);
      expect(pool?.reread).toBe(true);
    });
  });
});

// --- pool-backed checks ---

const repos = new Repositories(openDb({ path: ':memory:', memory: true }));
const cfg = ConfigSchema.parse({ mode: 'paper' });

function ctxWith(pool: PoolInfo | undefined, creatorShare?: number): CheckContext {
  const graduation: GraduationEvent = { mint: 'M', venue: 'pumpswap', poolAddress: '', slot: 1, feedSource: 'pumpportal', receivedAtNs: 0n };
  const candidate: Candidate = {
    graduation,
    enrichment: {
      unknowns: [],
      elapsedMs: 1,
      ...(pool ? { pool } : {}),
      ...(creatorShare !== undefined ? { creatorHolding: { creator: 'DEV', share: creatorShare } } : {}),
    },
  };
  return { candidate, config: cfg, repos, mode: 'paper', walletSol: 0 };
}

const POOL: PoolInfo = {
  poolAddress: 'POOL', baseMint: 'M', quoteMint: WSOL_MINT, lpMint: 'LP',
  baseVault: 'BASEVAULT', quoteVault: 'QUOTEVAULT', creator: 'C', coinCreator: 'DEV',
  isCanonical: true, baseReserve: 1000n, quoteReserveLamports: BigInt(50) * BigInt(LAMPORTS_PER_SOL),
};

describe('H7 liquidity floor + impact', () => {
  it('passes a deep pool with low impact', () => {
    expect(checkLiquidityFloor(ctxWith(POOL)).status).toBe('pass'); // 50 SOL, dust-size impact
  });
  it('fails below the SOL floor', () => {
    expect(checkLiquidityFloor(ctxWith({ ...POOL, quoteReserveLamports: BigInt(10) * BigInt(LAMPORTS_PER_SOL) })).status).toBe('fail');
  });
  it('fails without a pool (never unknown)', () => {
    expect(checkLiquidityFloor(ctxWith(undefined)).status).toBe('fail');
  });
});

describe('H6 creator holdings', () => {
  it('fails when the dev holds over the cap', () => {
    expect(checkCreatorHoldings(ctxWith(POOL, 0.1)).status).toBe('fail'); // 10% > 5%
  });
  it('passes when the dev holds little', () => {
    expect(checkCreatorHoldings(ctxWith(POOL, 0.02)).status).toBe('pass');
  });
  it('does not veto when the creator bag was not read', () => {
    expect(checkCreatorHoldings(ctxWith(POOL))).toMatchObject({ status: 'pass', reason: 'not_checked' });
  });
});
