import { describe, it, expect } from 'vitest';
import type { Connection } from '@solana/web3.js';
import { ExitLadder } from '../src/positions/presign.ts';
import { Wallet } from '../src/executor/wallet.ts';
import type { PumpAmmClient } from '../src/executor/pumpAmm.ts';

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

function makeLadder(now: () => number) {
  const wallet = Wallet.load('NONEXISTENT_ENV', 'dry-run'); // ephemeral keypair
  return new ExitLadder({
    connection: fakeConnection,
    wallet,
    pumpAmm: fakePumpAmm,
    feePlanProvider: async () => ({ priorityMicroLamports: 1000, jitoTipLamports: 0 }),
    poolAddress: 'pool',
    baseMint: 'mint',
    slippageTiers: [2, 5, 10, 25],
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
});
