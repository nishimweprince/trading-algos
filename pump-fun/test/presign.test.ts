import { afterEach, describe, it, expect, vi } from 'vitest';
import { TransactionMessage, type AddressLookupTableAccount, type Connection } from '@solana/web3.js';
import { ExitLadder } from '../src/positions/presign.ts';
import { Wallet } from '../src/executor/wallet.ts';
import type { PumpAmmClient } from '../src/executor/pumpAmm.ts';
import { assembleSignedSwapTx } from '../src/executor/assemble.ts';

// Offline harness: mock the connection's blockhash + an empty swap builder so
// the ladder assembles real (compute-budget-only) signed txs without a network.
const fakeConnection = {
  getLatestBlockhash: async () => ({
    blockhash: '11111111111111111111111111111111',
    lastValidBlockHeight: 1,
  }),
} as unknown as Connection;

const fakePumpAmm = {
  buildSell: async () => [],
} as unknown as PumpAmmClient;

afterEach(() => {
  vi.restoreAllMocks();
});

function makeLadder(now: () => number, emergencySlippagePct?: number) {
  const wallet = Wallet.load('NONEXISTENT_ENV', 'dry-run'); // ephemeral keypair
  return new ExitLadder({
    connection: fakeConnection,
    wallet,
    pumpAmm: fakePumpAmm,
    feePlanProvider: async () => ({ priorityMicroLamports: 1000, jitoTipLamports: 0 }),
    poolAddress: 'pool',
    baseMint: 'mint',
    slippageTiers: [2, 5, 10, 25],
    emergencySlippagePct,
    now,
  });
}

describe('ExitLadder', () => {
  it('refreshes a signed tx per slippage tier', async () => {
    const ladder = makeLadder(() => 1000);
    await ladder.refresh(1_000_000n);
    expect(ladder.size).toBe(4);
    expect(ladder.amount).toBe(1_000_000n);
  });

  it('picks the tightest tier that tolerates the target slippage', async () => {
    const ladder = makeLadder(() => 1000);
    await ladder.refresh(1_000_000n);
    expect(ladder.pick(2)?.slippagePct).toBe(2);
    expect(ladder.pick(3)?.slippagePct).toBe(5); // 2% too tight -> next up
    expect(ladder.pick(6)?.slippagePct).toBe(10);
    expect(ladder.pick(30)?.slippagePct).toBe(25); // beyond all -> worst
  });

  it('emergency picks the worst tier; escalation walks looser', async () => {
    const ladder = makeLadder(() => 1000);
    await ladder.refresh(1_000_000n);
    expect(ladder.worst()?.slippagePct).toBe(25);
    expect(ladder.next(5)?.slippagePct).toBe(10);
    expect(ladder.next(25)).toBeNull(); // nothing looser than the worst
  });

  it('reports staleness past the refresh window', async () => {
    let t = 1000;
    const ladder = makeLadder(() => t);
    await ladder.refresh(1_000_000n);
    expect(ladder.isStale(45_000)).toBe(false);
    t = 1000 + 46_000;
    expect(ladder.isStale(45_000)).toBe(true);
  });

  it('clears to empty when there is nothing to sell', async () => {
    const ladder = makeLadder(() => 1000);
    await ladder.refresh(0n);
    expect(ladder.size).toBe(0);
    expect(ladder.worst()).toBeNull();
    expect(ladder.isStale(45_000)).toBe(true);
  });

  it('passes optional address lookup tables into v0 message compilation', async () => {
    const wallet = Wallet.load('NONEXISTENT_ENV', 'dry-run');
    const original = TransactionMessage.prototype.compileToV0Message;
    const spy = vi.spyOn(TransactionMessage.prototype, 'compileToV0Message');
    spy.mockImplementation(function (this: TransactionMessage, lookupTables) {
      return original.call(this, lookupTables);
    });
    const lookupTables: AddressLookupTableAccount[] = [];

    await assembleSignedSwapTx([], {
      connection: fakeConnection,
      wallet,
      feePlan: { priorityMicroLamports: 1000, jitoTipLamports: 0 },
      addressLookupTableAccounts: lookupTables,
    });

    expect(spy).toHaveBeenCalledWith(lookupTables);
  });
});

describe('ExitLadder emergency tier', () => {
  it('builds one extra emergency-only tier that ordinary selection never returns', async () => {
    const ladder = makeLadder(() => 1000, 90);
    await ladder.refresh(1_000_000n);
    expect(ladder.size).toBe(5);
    // Ordinary escalation still ends at 25 — a TP/trailing exit never sees 90.
    expect(ladder.pick(30)?.slippagePct).toBe(25);
    expect(ladder.worst()?.slippagePct).toBe(25);
    expect(ladder.next(25)).toBeNull();
    // Emergencies go straight to 90.
    expect(ladder.emergency()?.slippagePct).toBe(90);
    expect(ladder.emergency()?.emergencyOnly).toBe(true);
  });

  it('falls back to the loosest ordinary tier when no emergency tier is configured or it is not looser', async () => {
    const none = makeLadder(() => 1000);
    await none.refresh(1_000_000n);
    expect(none.size).toBe(4);
    expect(none.emergency()?.slippagePct).toBe(25);
    const notLooser = makeLadder(() => 1000, 20);
    await notLooser.refresh(1_000_000n);
    expect(notLooser.size).toBe(4);
    expect(notLooser.emergency()?.slippagePct).toBe(25);
  });
});

describe('ExitLadder blockhash cost', () => {
  /**
   * Each tier used to pay its own getLatestBlockhash — 4 serial round trips per
   * refresh, every exits.ladderRefreshMs, per open position, and once
   * synchronously while opening a live position (where it delayed the first
   * price tick).
   */
  function countingConnection() {
    const state = { calls: 0 };
    const connection = {
      getLatestBlockhash: async () => {
        state.calls++;
        return { blockhash: '11111111111111111111111111111111', lastValidBlockHeight: 1 };
      },
    } as unknown as Connection;
    return { connection, state };
  }

  it('pays one blockhash fetch for a 4-tier refresh when a provider is supplied', async () => {
    const { connection, state } = countingConnection();
    const { BlockhashCache } = await import('../src/executor/blockhashCache.ts');
    const cache = new BlockhashCache(connection, 10_000, () => 0);
    const wallet = Wallet.load('NONEXISTENT_ENV', 'dry-run');
    const ladder = new ExitLadder({
      connection,
      wallet,
      pumpAmm: fakePumpAmm,
      feePlanProvider: async () => ({ priorityMicroLamports: 1000, jitoTipLamports: 0 }),
      poolAddress: 'pool',
      baseMint: 'mint',
      slippageTiers: [2, 5, 10, 25],
      blockhashProvider: () => cache.get(),
      now: () => 1000,
    });

    await ladder.refresh(1_000_000n);

    expect(ladder.size).toBe(4);
    expect(state.calls).toBe(1);
  });

  it('still pays one fetch per tier without a provider (documents the old cost)', async () => {
    const { connection, state } = countingConnection();
    const wallet = Wallet.load('NONEXISTENT_ENV', 'dry-run');
    const ladder = new ExitLadder({
      connection,
      wallet,
      pumpAmm: fakePumpAmm,
      feePlanProvider: async () => ({ priorityMicroLamports: 1000, jitoTipLamports: 0 }),
      poolAddress: 'pool',
      baseMint: 'mint',
      slippageTiers: [2, 5, 10, 25],
      now: () => 1000,
    });

    await ladder.refresh(1_000_000n);

    expect(state.calls).toBe(4);
  });
});
