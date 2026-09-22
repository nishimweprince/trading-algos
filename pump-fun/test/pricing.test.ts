import { describe, expect, it, vi } from 'vitest';
import {
  PricePoller,
  computePrice,
  type PoolRef,
  type PriceTick,
  type PricePollerOptions,
} from '../src/positions/pricing.ts';
import type { RpcClient } from '../src/core/rpc.ts';

/**
 * PricePoller had no direct test, which is how it shipped reading vaults at a
 * different commitment than the entry path and swallowing every failure at
 * `debug`. 14 of 25 live positions exited on a single price observation.
 */

type Acct = { data: string; owner: string; lamports: number; executable: boolean } | null;

/** SPL token account layout: amount is a u64 LE at offset 64. */
function tokenAccount(amount: bigint): Acct {
  const buf = Buffer.alloc(165);
  buf.writeBigUInt64LE(amount, 64);
  return { data: buf.toString('base64'), owner: 'token-program', lamports: 0, executable: false };
}

class FakeRpc {
  calls: Array<{ pubkeys: string[]; commitment: string | undefined }> = [];
  /** address -> account (absent = null, i.e. "not visible at this commitment"). */
  accounts = new Map<string, Acct>();
  failWith: Error | null = null;
  /** When set, the read never settles — simulates an unbounded semaphore queue. */
  hang = false;

  async getMultipleAccountsBase64(pubkeys: string[], commitment?: string): Promise<Acct[]> {
    this.calls.push({ pubkeys, commitment });
    if (this.hang) return new Promise<Acct[]>(() => {});
    if (this.failWith) throw this.failWith;
    return pubkeys.map((p) => this.accounts.get(p) ?? null);
  }
}

function ref(mint: string, withCreator = false): PoolRef {
  return {
    mint,
    baseVault: `${mint}-base`,
    quoteVault: `${mint}-quote`,
    baseDecimals: 6,
    ...(withCreator ? { creatorAta: `${mint}-creator` } : {}),
  };
}

function seed(rpc: FakeRpc, mint: string, base = 1_000_000_000n, quote = 5_000_000_000n, withCreator = false) {
  rpc.accounts.set(`${mint}-base`, tokenAccount(base));
  rpc.accounts.set(`${mint}-quote`, tokenAccount(quote));
  if (withCreator) rpc.accounts.set(`${mint}-creator`, tokenAccount(7n));
}

function build(rpc: FakeRpc, opts: PricePollerOptions = {}, nowMs = { v: 0 }) {
  const ticks: PriceTick[] = [];
  const poller = new PricePoller(rpc as unknown as RpcClient, 500, () => nowMs.v, opts);
  poller.setHandler((t) => ticks.push(t));
  return { poller, ticks };
}

/** Drive one poll cycle without the interval. */
async function poll(poller: PricePoller): Promise<void> {
  await (poller as unknown as { poll(): Promise<void> }).poll();
}

describe('PricePoller commitment', () => {
  it('reads vaults at the configured commitment in poll()', async () => {
    const rpc = new FakeRpc();
    seed(rpc, 'A');
    const { poller, ticks } = build(rpc, { commitment: 'processed' });
    poller.register(ref('A'));
    await poll(poller);

    expect(rpc.calls).toHaveLength(1);
    expect(rpc.calls[0]!.commitment).toBe('processed');
    expect(ticks).toHaveLength(1);
  });

  it('reads vaults at the configured commitment in readOnce()', async () => {
    const rpc = new FakeRpc();
    seed(rpc, 'A');
    const { poller } = build(rpc, { commitment: 'processed' });

    const read = await poller.readOnce(ref('A'));

    expect(rpc.calls[0]!.commitment).toBe('processed');
    expect(read?.price).toBe(computePrice(1_000_000_000n, 5_000_000_000n, 6));
  });
});

describe('PricePoller batching', () => {
  it('chunks past the 100-pubkey getMultipleAccounts cap and keeps reserves aligned', async () => {
    const rpc = new FakeRpc();
    const mints = Array.from({ length: 40 }, (_, i) => `M${i}`);
    // 40 refs x 3 accounts = 120 pubkeys, which a single call would reject.
    mints.forEach((m, i) => seed(rpc, m, BigInt(1_000_000 * (i + 1)), BigInt(2_000_000 * (i + 1)), true));
    const { poller, ticks } = build(rpc, { batchSize: 100 });
    mints.forEach((m) => poller.register(ref(m, true)));

    await poll(poller);

    expect(rpc.calls).toHaveLength(2);
    for (const c of rpc.calls) expect(c.pubkeys.length).toBeLessThanOrEqual(100);
    expect(ticks).toHaveLength(40);
    // Alignment across the chunk boundary is the thing that breaks silently.
    for (const [i, m] of mints.entries()) {
      const tick = ticks.find((t) => t.mint === m)!;
      expect(tick.baseReserve).toBe(BigInt(1_000_000 * (i + 1)));
      expect(tick.quoteReserveLamports).toBe(BigInt(2_000_000 * (i + 1)));
      expect(tick.creatorBaseBalance).toBe(7n);
    }
  });
});

describe('PricePoller starvation', () => {
  it('releases the in-flight guard at the deadline so the next cycle can run', async () => {
    vi.useFakeTimers();
    try {
      const rpc = new FakeRpc();
      seed(rpc, 'A');
      rpc.hang = true;
      const { poller } = build(rpc, { deadlineMs: 2_000 });
      poller.register(ref('A'));

      const first = poll(poller);
      await vi.advanceTimersByTimeAsync(2_001);
      await first;

      expect(poller.pollStats.deadlineExpired).toBe(1);

      // Before the deadline existed, `polling` stayed true and every later
      // cycle was dropped for every position.
      rpc.hang = false;
      await poll(poller);
      expect(rpc.calls).toHaveLength(2);
    } finally {
      vi.useRealTimers();
    }
  });

  it('attributes a missing vault per mint without costing the others a tick', async () => {
    const rpc = new FakeRpc();
    seed(rpc, 'B');
    // A's vaults are not visible — the pre-fix code did a bare `continue`.
    const { poller, ticks } = build(rpc);
    poller.register(ref('A'));
    poller.register(ref('B'));

    await poll(poller);

    expect(ticks.map((t) => t.mint)).toEqual(['B']);
    expect(poller.healthFor('A')).toMatchObject({ consecutiveMisses: 1, ticks: 0, lastTickAtMs: null });
    expect(poller.healthFor('B')).toMatchObject({ consecutiveMisses: 0, ticks: 1 });

    // Recovery resets the streak.
    seed(rpc, 'A');
    await poll(poller);
    expect(poller.healthFor('A')).toMatchObject({ consecutiveMisses: 0, ticks: 1 });
  });

  it('counts a thrown batch read and reports every position as missed', async () => {
    const rpc = new FakeRpc();
    seed(rpc, 'A');
    rpc.failWith = new Error('429 rate limited');
    const { poller, ticks } = build(rpc);
    poller.register(ref('A'));

    await poll(poller);

    expect(ticks).toHaveLength(0);
    expect(poller.pollStats.failures).toBe(1);
    expect(poller.pollStats.lastErr).toContain('429');
  });

  it('counts overlap skips instead of dropping cycles silently', async () => {
    const rpc = new FakeRpc();
    seed(rpc, 'A');
    rpc.hang = true;
    const { poller } = build(rpc);
    poller.register(ref('A'));

    void poll(poller); // holds the guard
    await poll(poller); // skipped
    await poll(poller); // skipped

    expect(poller.pollStats.overlapSkips).toBe(2);
    expect(rpc.calls).toHaveLength(1);
  });

  it('tracks per-mint registration time and tick recency for the blind guard', async () => {
    const rpc = new FakeRpc();
    seed(rpc, 'B');
    const now = { v: 1_000 };
    const { poller } = build(rpc, {}, now);
    poller.register(ref('A'));
    poller.register(ref('B'));

    now.v = 2_000;
    await poll(poller);

    expect(poller.healthFor('A')).toMatchObject({ registeredAtMs: 1_000, lastTickAtMs: null });
    expect(poller.healthFor('B')).toMatchObject({ registeredAtMs: 1_000, lastTickAtMs: 2_000 });
    poller.unregister('A');
    expect(poller.healthFor('A')).toBeNull();
  });
});

describe('computePrice', () => {
  it('returns 0 on an empty base reserve — the value the suspect-tick guard rejects', () => {
    expect(computePrice(0n, 5_000_000_000n, 6)).toBe(0);
  });
});
