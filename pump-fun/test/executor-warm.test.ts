import { describe, expect, it, vi, afterEach } from 'vitest';
import { createHash, randomBytes } from 'node:crypto';
import { Keypair, PublicKey } from '@solana/web3.js';
import { PUMP_AMM_SDK, canonicalPumpPoolPda } from '@pump-fun/pump-swap-sdk';
import { ConfigSchema } from '../src/config/schema.ts';
import type { RpcClient } from '../src/core/rpc.ts';
import { base58Encode } from '../src/core/base58.ts';
import { deriveAta } from '../src/core/ata.ts';
import { PROGRAM_IDS, WSOL_MINT } from '../src/core/constants.ts';
import { BlockhashCache } from '../src/executor/blockhashCache.ts';
import { Executor } from '../src/executor/index.ts';
import { PumpAmmClient, decodeBuyBaseOut } from '../src/executor/pumpAmm.ts';
import { Wallet } from '../src/executor/wallet.ts';
import { PrefetchedSwapStates, buildSwapState, type SwapStateParts } from '../src/executor/swapState.ts';
import type { FeePlan } from '../src/executor/fees.ts';

const disc = (name: string) => createHash('sha256').update(`account:${name}`).digest().subarray(0, 8);

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

const configs = () => {
  const g = Buffer.alloc(1000);
  disc('GlobalConfig').copy(g, 0);
  return {
    globalConfig: PUMP_AMM_SDK.decodeGlobalConfig({
      data: g,
      owner: new PublicKey(PROGRAM_IDS.PUMP_SWAP),
      lamports: 1,
      executable: false,
    }),
    feeConfig: null,
  };
};

const throwingRpc = () =>
  new Proxy(
    {},
    {
      get: () => {
        throw new Error('network touched');
      },
    },
  ) as unknown as RpcClient;

function testExecutor() {
  const config = ConfigSchema.parse({
    mode: 'dry-run',
    rpc: { primaryHttp: 'http://127.0.0.1:9' },
    execution: { stateCommitment: 'processed' },
  });
  return new Executor({ config, rpc: throwingRpc(), httpUrl: 'http://127.0.0.1:9' });
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('PumpAmmClient last-quote replay', () => {
  it('records the last quote and reproduces it offline with identical outputs', async () => {
    const cache = new PrefetchedSwapStates();
    const p = parts();
    cache.put(buildSwapState(p, configs()));
    const client = new PumpAmmClient('http://127.0.0.1:9', 'processed', { prefetched: cache, timeoutMs: 50 });
    expect(client.lastBuyQuote()).toBeNull();
    const quoted = await client.buildBuyQuoted(p.poolKey, new PublicKey(p.user), 40_000_000n, 5);
    const last = client.lastBuyQuote();
    expect(last).not.toBeNull();
    expect(last!.quoteLamports).toBe(40_000_000n);
    expect(last!.slippagePct).toBe(5);
    const replay = await client.buyQuoteFromState(last!.state, last!.quoteLamports, last!.slippagePct);
    expect(replay).toHaveLength(quoted.ixs.length);
    expect(decodeBuyBaseOut(replay)).toBe(decodeBuyBaseOut(quoted.ixs));
  });
});

describe('BlockhashCache.peek', () => {
  it('serves the fresh value and null when empty or stale, without networking', async () => {
    let now = 1_000;
    const cache = new BlockhashCache({} as never, 10_000, () => now);
    expect(cache.peek()).toBeNull();
    await cache.warm().catch(() => undefined);
    expect(cache.peek()).toBeNull(); // refresh failed with no connection; still null, never throws
    const live = new BlockhashCache(
      { getLatestBlockhash: async () => ({ blockhash: 'B'.repeat(44), lastValidBlockHeight: 1 }) } as never,
      10_000,
      () => now,
    );
    await live.warm();
    expect(live.peek()).toBe('B'.repeat(44));
    now += 10_001;
    expect(live.peek()).toBeNull();
  });
});

describe('Executor.warmBuyPath', () => {
  const feePlan: FeePlan = { priorityMicroLamports: 500_000, jitoTipLamports: 0, exitTipLamports: 0 };

  it('returns false on a cold executor without touching the network', async () => {
    vi.stubGlobal(
      'fetch',
      (() => {
        throw new Error('network touched');
      }) as never,
    );
    const exec = testExecutor();
    await expect(exec.warmBuyPath()).resolves.toBe(false);
    expect(exec.warmBuyRuns).toBe(0);
  });

  it('replays quote → assemble → sign offline and drops the bytes', async () => {
    vi.stubGlobal(
      'fetch',
      (() => {
        throw new Error('network touched');
      }) as never,
    );
    const exec = testExecutor();
    const p = parts();
    const state = buildSwapState(p, configs());
    const blockhash = base58Encode(randomBytes(32));
    const quote = { state, quoteLamports: 40_000_000n, slippagePct: 5 };
    await expect(exec.warmBuyPath({ quote, feePlan, blockhash })).resolves.toBe(true);
    await expect(exec.warmBuyPath({ quote, feePlan, blockhash })).resolves.toBe(true);
    expect(exec.warmBuyRuns).toBe(2);
  });
});

describe('buyWarmMs default', () => {
  it('warms every second unless disabled', () => {
    expect(ConfigSchema.parse({}).execution.buyWarmMs).toBe(1_000);
    expect(ConfigSchema.parse({ execution: { buyWarmMs: 0 } }).execution.buyWarmMs).toBe(0);
  });
});

describe('Wallet.ephemeral', () => {
  it('mints distinct never-funded keypairs', () => {
    const a = Wallet.ephemeral();
    const b = Wallet.ephemeral();
    expect(a.ephemeral).toBe(true);
    expect(a.publicKey).not.toBe(b.publicKey);
  });
});
