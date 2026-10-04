import { describe, expect, it, vi } from 'vitest';
import BN from 'bn.js';
import { ComputeBudgetProgram, PublicKey, SystemProgram, VersionedTransaction } from '@solana/web3.js';
import { Executor } from '../src/executor/index.ts';
import type { RpcClient } from '../src/core/rpc.ts';
import { ConfigSchema } from '../src/config/schema.ts';
import { PositionManager } from '../src/positions/manager.ts';
import type { PricePoller, PriceTick, PoolRef } from '../src/positions/pricing.ts';
import { TypedBus } from '../src/core/bus.ts';
import { Repositories } from '../src/persistence/repositories.ts';
import { openDb } from '../src/persistence/db.ts';
import type { BroadcastResult } from '../src/executor/broadcaster.ts';
import type { FillActuals } from '../src/executor/fillActuals.ts';

const MINT = 'Mint1111111111111111111111111111111111111111';

function executorWithFakes() {
  const config = ConfigSchema.parse({
    mode: 'dry-run',
    rpc: { primaryHttp: 'http://127.0.0.1:1' },
    wallet: { keypairEnvVar: 'PUMPDESK_TEST_NO_SUCH_KEY' },
    execution: { blockhashCacheMs: 10_000 },
    fees: { exitPriorityMultiplier: 2, priorityCapMicroLamports: 5_000_000 },
    exits: { stateMaxAgeMs: 60_000 },
  });
  const ex = new Executor({ config, rpc: {} as RpcClient, httpUrl: 'http://127.0.0.1:1' });
  const internals = ex as unknown as Record<string, unknown>;
  let captured: { poolBaseAmount: BN; poolQuoteAmount: BN } | null = null;
  const fakeAmm = {
    swapState: vi.fn(async () => ({ poolBaseAmount: new BN(1), poolQuoteAmount: new BN(1), pool: 'static' })),
    buildSellFromState: vi.fn(async (state: { poolBaseAmount: BN; poolQuoteAmount: BN }) => {
      captured = state;
      return [SystemProgram.transfer({ fromPubkey: new PublicKey(ex.publicKey), toPubkey: new PublicKey(ex.publicKey), lamports: 1 })];
    }),
  };
  internals.pumpAmm = fakeAmm;
  internals.blockhashes = { get: vi.fn(async () => '11111111111111111111111111111111'), warm: vi.fn(), invalidate: vi.fn() };
  internals.feePlanCache = { atMs: Date.now(), plan: { priorityMicroLamports: 300_000, jitoTipLamports: 0 } };
  const getLatestBlockhash = vi.fn();
  (internals.connection as { getLatestBlockhash: unknown }).getLatestBlockhash = getLatestBlockhash;
  return { ex, internals, fakeAmm, getLatestBlockhash, captured: () => captured };
}

function priorityOf(bytes: Uint8Array): bigint | null {
  const tx = VersionedTransaction.deserialize(bytes);
  const keys = tx.message.staticAccountKeys;
  for (const ix of tx.message.compiledInstructions) {
    if (keys[ix.programIdIndex]!.equals(ComputeBudgetProgram.programId) && ix.data[0] === 3) {
      return Buffer.from(ix.data).readBigUInt64LE(1);
    }
  }
  return null;
}

describe('Executor.buildExitTx — trigger-time sell from live reserves', () => {
  it('patches the triggering tick\'s reserves into the cached state, signs with cached blockhash, no network', async () => {
    const h = executorWithFakes();
    await h.ex.primeExitState(MINT, 'pool');
    h.fakeAmm.swapState.mockClear();
    const bytes = await h.ex.buildExitTx(MINT, 1_000n, 20, { baseReserve: 777_000_000n, quoteReserveLamports: 85_000_000_000n });
    expect(bytes).not.toBeNull();
    expect(h.captured()!.poolBaseAmount.toString()).toBe('777000000');
    expect(h.captured()!.poolQuoteAmount.toString()).toBe('85000000000');
    expect(h.fakeAmm.buildSellFromState).toHaveBeenCalledWith(expect.anything(), 1_000n, 20);
    expect(h.fakeAmm.swapState).not.toHaveBeenCalled(); // no state read on the hot path
    expect(h.getLatestBlockhash).not.toHaveBeenCalled(); // cached blockhash
    expect(priorityOf(bytes!)).toBe(600_000n); // exit-boosted 2x
  });

  it('returns null (caller falls back) with no cached state, stale state, or empty reserves', async () => {
    const h = executorWithFakes();
    expect(await h.ex.buildExitTx(MINT, 1n, 20, { baseReserve: 1n, quoteReserveLamports: 1n })).toBeNull();
    await h.ex.primeExitState(MINT, 'pool');
    expect(await h.ex.buildExitTx(MINT, 1n, 20, { baseReserve: 0n, quoteReserveLamports: 1n })).toBeNull();
    (h.internals.exitStates as Map<string, { atMs: number }>).get(MINT)!.atMs = Date.now() - 61_000;
    expect(await h.ex.buildExitTx(MINT, 1n, 20, { baseReserve: 1n, quoteReserveLamports: 1n })).toBeNull();
  });

  it('caps the exit priority at priorityCapMicroLamports', async () => {
    const h = executorWithFakes();
    h.internals.feePlanCache = { atMs: Date.now(), plan: { priorityMicroLamports: 4_000_000, jitoTipLamports: 0 } };
    await h.ex.primeExitState(MINT, 'pool');
    const bytes = await h.ex.buildExitTx(MINT, 1n, 20, { baseReserve: 1n, quoteReserveLamports: 1n });
    expect(priorityOf(bytes!)).toBe(5_000_000n);
  });
});

class FakePoller {
  handler: (t: PriceTick) => void = () => {};
  setHandler(h: (t: PriceTick) => void) { this.handler = h; }
  register(_r: PoolRef) {}
  unregister() {}
  async readOnce() { return null; }
  start() {}
  stop() {}
}

describe('ExitSupervisor uses the trigger-time builder with the live-price slippage policy', () => {
  const ok = (signature: string): BroadcastResult => ({ mode: 'live', simulated: false, sent: true, confirmed: true, signature, attempts: [] });
  const sold: FillActuals = { signature: 'exit', slot: 1, walletLamportsDelta: 10_000_000, feeLamports: 5_000, rentLamports: 0, tokenRawDelta: -2_500_000_000_000n, err: null };

  function run(triggerPrice: number) {
    const cfg = ConfigSchema.parse({ mode: 'live', rpc: { primaryHttp: 'https://rpc.example' }, exits: { presignLadder: false } });
    const buildExitTx = vi.fn(async () => new Uint8Array([1]));
    const sellAndConfirm = vi.fn(async () => ok('fresh'));
    const executor = {
      buyAndConfirm: vi.fn(async () => ok('buy')),
      reconcileTokenBalance: vi.fn(async () => 2_500_000_000_000n),
      primeExitState: vi.fn(async () => undefined),
      dropExitState: vi.fn(),
      buildExitTx,
      broadcastSignedExit: vi.fn(async () => ok('exit')),
      sellAndConfirm,
      fillActuals: vi.fn(async (sig: string) => (sig === 'exit' ? sold : null)),
    };
    const poller = new FakePoller();
    const bus = new TypedBus();
    const mgr = new PositionManager({ config: cfg, bus, repos: new Repositories(openDb({ path: ':memory:', memory: true })), poller: poller as unknown as PricePoller, executor: executor as never });
    mgr.start();
    const pricing = { poolAddress: 'pool', baseMint: 'mint', baseVault: 'bv', quoteVault: 'qv', baseDecimals: 6, baseReserve: 10n ** 15n, quoteReserveLamports: 10n ** 11n };
    return { executor, buildExitTx, sellAndConfirm, poller, bus, mgr, pricing, triggerPrice };
  }
  const settle = async () => {
    for (let i = 0; i < 8; i++) await new Promise((r) => setTimeout(r, 0));
    await new Promise((r) => setTimeout(r, 20));
  };

  it('a stop-loss sells via the builder at 20 % against the triggering tick reserves; state is primed at open', async () => {
    const h = run(2e-9);
    h.bus.emit('openPosition', { mint: 'M', sizeSol: 0.02, highVolatility: false, pricing: h.pricing });
    await settle();
    expect(h.executor.primeExitState).toHaveBeenCalledWith('mint', 'pool');
    h.poller.handler({ mint: 'M', price: 2e-9, baseReserve: 4n * 10n ** 15n, quoteReserveLamports: 8n * 10n ** 9n, atMs: 1000 });
    await settle();
    expect(h.buildExitTx).toHaveBeenCalledWith('mint', 2_500_000_000_000n, 20, { baseReserve: 4n * 10n ** 15n, quoteReserveLamports: 8n * 10n ** 9n });
    expect(h.sellAndConfirm).not.toHaveBeenCalled();
    expect(h.executor.dropExitState).toHaveBeenCalledWith('mint');
    h.mgr.stop();
  });

  it('a take-profit starts at 8 %', async () => {
    const h = run(1e-8);
    h.bus.emit('openPosition', { mint: 'M', sizeSol: 0.02, highVolatility: false, pricing: h.pricing });
    await settle();
    h.poller.handler({ mint: 'M', price: 2e-8, baseReserve: 10n ** 15n, quoteReserveLamports: 10n ** 13n, atMs: 1000 }); // +150 %: past any TP default
    await settle();
    expect((h.buildExitTx.mock.calls[0] as unknown[])[2]).toBe(8);
    h.mgr.stop();
  });

  it('falls back to a fresh build when no tick has carried reserves yet', async () => {
    const h = run(2e-9);
    h.bus.emit('openPosition', { mint: 'M', sizeSol: 0.02, highVolatility: false, pricing: h.pricing });
    await settle();
    h.poller.handler({ mint: 'M', price: 2e-9, baseReserve: 0n, quoteReserveLamports: 0n, atMs: 1000 });
    await settle();
    expect(h.buildExitTx).not.toHaveBeenCalled();
    expect(h.sellAndConfirm).toHaveBeenCalled();
    h.mgr.stop();
  });
});
