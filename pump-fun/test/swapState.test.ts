import { describe, it, expect } from 'vitest';
import { createHash } from 'node:crypto';
import { Keypair, PublicKey } from '@solana/web3.js';
import BN from 'bn.js';
import { PUMP_AMM_SDK, PumpAmmSdk, canonicalPumpPoolPda } from '@pump-fun/pump-swap-sdk';
import { AmmConfigCache, PrefetchedSwapStates, buildSwapState, type RawAccountRead, type SwapStateParts } from '../src/executor/swapState.ts';
import { PumpAmmClient, decodeBuyBaseOut } from '../src/executor/pumpAmm.ts';
import { deriveAta } from '../src/core/ata.ts';
import { PROGRAM_IDS, WSOL_MINT } from '../src/core/constants.ts';

const disc = (name: string) => createHash('sha256').update(`account:${name}`).digest().subarray(0, 8);

function globalConfigAccount(): RawAccountRead {
  const g = Buffer.alloc(1000);
  disc('GlobalConfig').copy(g, 0);
  return { data: g.toString('base64'), owner: PROGRAM_IDS.PUMP_SWAP, lamports: 1 };
}

function parts(user = Keypair.generate().publicKey.toBase58()): SwapStateParts {
  const mint = Keypair.generate().publicKey.toBase58();
  const pool = canonicalPumpPoolPda(new PublicKey(mint)).toBase58();
  const p = Buffer.alloc(300);
  disc('Pool').copy(p, 0);
  new PublicKey(mint).toBuffer().copy(p, 43);
  new PublicKey(WSOL_MINT).toBuffer().copy(p, 75);
  new PublicKey(deriveAta(pool, mint, false)).toBuffer().copy(p, 139);
  new PublicKey(deriveAta(pool, WSOL_MINT, false)).toBuffer().copy(p, 171);
  Keypair.generate().publicKey.toBuffer().copy(p, 211); // coin_creator
  const m = Buffer.alloc(82);
  m.writeBigUInt64LE(1_000_000_000_000_000n, 36);
  m.writeUInt8(6, 44);
  m.writeUInt8(1, 45);
  return {
    poolKey: pool,
    pool: { data: p.toString('base64'), owner: PROGRAM_IDS.PUMP_SWAP, lamports: 1 },
    baseMint: { data: m.toString('base64'), owner: PROGRAM_IDS.TOKEN, lamports: 1 },
    baseReserve: 206_900_000_000_000n,
    quoteReserveLamports: 84_000_000_000n,
    user,
    userBaseAta: deriveAta(user, mint, false),
    userBaseAccount: null,
    userQuoteAta: deriveAta(user, WSOL_MINT, false),
    userQuoteAccount: null,
  };
}

const configs = () => ({
  globalConfig: PUMP_AMM_SDK.decodeGlobalConfig({
    data: Buffer.from(globalConfigAccount().data, 'base64'),
    owner: new PublicKey(PROGRAM_IDS.PUMP_SWAP),
    lamports: 1,
    executable: false,
  }),
  feeConfig: null,
});

describe('offline swap state', () => {
  it('builds a state the SDK turns into a PumpSwap buy with no RPC', async () => {
    const state = buildSwapState(parts(), configs());
    expect(state.poolBaseAmount.toString()).toBe('206900000000000');
    const ixs = await new PumpAmmSdk().buyQuoteInput(state, new BN(40_000_000), 5);
    expect(ixs.some((ix) => ix.programId.toBase58() === PROGRAM_IDS.PUMP_SWAP)).toBe(true);
    expect(decodeBuyBaseOut(ixs)).toBeGreaterThan(0n);
  });

  it('serves a prefetched state only to its user and only while fresh', () => {
    let now = 0;
    const cache = new PrefetchedSwapStates(3_000, () => now);
    const p = parts();
    cache.put(buildSwapState(p, configs()));
    expect(cache.get(p.poolKey, new PublicKey(p.user))).not.toBeNull();
    expect(cache.get(p.poolKey, Keypair.generate().publicKey)).toBeNull();
    now = 3_001;
    expect(cache.get(p.poolKey, new PublicKey(p.user))).toBeNull();
  });

  it('PumpAmmClient quotes a buy from the prefetched state without touching the network', async () => {
    const cache = new PrefetchedSwapStates();
    const p = parts();
    cache.put(buildSwapState(p, configs()));
    // Unroutable endpoint: an online state read would throw.
    const client = new PumpAmmClient('http://127.0.0.1:9', 'processed', { prefetched: cache, timeoutMs: 50 });
    const quoted = await client.buildBuyQuoted(p.poolKey, new PublicKey(p.user), 40_000_000n, 5);
    expect(quoted).toMatchObject({ prefetched: true, quoteReserveLamports: 84_000_000_000n });
    expect(decodeBuyBaseOut(quoted.ixs)).toBeGreaterThan(0n);
  });
});

describe('AmmConfigCache', () => {
  it('decodes both configs in one read and keeps the last good value on a failed refresh', async () => {
    let fail = false;
    const calls: string[][] = [];
    const rpc = {
      getMultipleAccountsBase64: async (keys: string[]) => {
        calls.push(keys);
        if (fail) throw new Error('429');
        return [{ ...globalConfigAccount(), executable: false }, null];
      },
    };
    const cache = new AmmConfigCache(rpc);
    expect(cache.get()).toBeNull();
    await cache.refresh();
    expect(calls[0]).toHaveLength(2);
    const first = cache.get();
    expect(first?.globalConfig).toBeDefined();
    expect(first?.feeConfig).toBeNull();
    fail = true;
    await cache.refresh();
    expect(cache.get()).toBe(first);
  });
});
