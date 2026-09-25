import { describe, expect, it, vi } from 'vitest';
import { ConfigSchema } from '../src/config/schema.ts';
import { Executor } from '../src/executor/index.ts';
import { EntryMoveExceeded } from '../src/executor/slippage.ts';
import type { RpcClient } from '../src/core/rpc.ts';
import type { BroadcastResult } from '../src/executor/broadcaster.ts';

/**
 * Parallel speculative simulate (execution.parallelBuySimulate).
 *
 * Serially, learning that the tight tier fails slippage cost a whole extra
 * round — fresh state read, fresh assemble, fresh simulate — inside the window
 * where the price is moving. Here all tiers are quoted, assembled and simulated
 * concurrently and only the tightest that passed is SENT.
 *
 * The thing these tests pin down is that exactly ONE send happens. Each tier is
 * an independently valid transaction and sends use skipPreflight, so two landing
 * means buying 2x the intended size.
 */

const EXCEEDED_SLIPPAGE = { InstructionError: [7, { Custom: 6004 }] };
const BLOCKHASH = '11111111111111111111111111111111';

const sent = (signature: string): BroadcastResult => ({
  mode: 'dry-run',
  simulated: true,
  sent: true,
  confirmed: true,
  signature,
  route: 'primary',
  logs: [],
  attempts: [{ route: 'primary', submittedAtMs: 1, sent: true, signature }],
});

/**
 * `failTiers` lists the slippage bounds whose simulate returns 6004.
 * `reserves` lets a test drive the entry-move gate.
 */
function executorWith(opts: {
  failTiers?: number[];
  retryTiers?: number[];
  reserves?: { baseReserve: bigint; quoteReserveLamports: bigint };
  parallel?: boolean;
}) {
  const failTiers = new Set(opts.failTiers ?? []);
  const quoted: number[] = [];
  const simulated: number[] = [];
  const broadcasts: Array<{ skipSimulation: boolean | undefined; confirmTimeoutMs: number | undefined }> = [];

  const rpc = { getSignatureStatuses: async () => [] } as unknown as RpcClient;
  const config = ConfigSchema.parse({
    mode: 'dry-run', // ephemeral wallet; the broadcaster below is a stub anyway
    rpc: { primaryHttp: 'http://127.0.0.1:1' },
    entry: { buyRetrySlippageTiers: opts.retryTiers ?? [8] },
    execution: { parallelBuySimulate: opts.parallel ?? true },
  });
  const exec = new Executor({ config, rpc, httpUrl: 'http://127.0.0.1:1' });

  // Slippage is encoded into the assembled bytes only via the swap ixs, which
  // are empty here — so the simulate stub is keyed off call order instead.
  const internals = exec as unknown as {
    pumpAmm: unknown;
    broadcaster: unknown;
    blockhashes: unknown;
  };
  const pendingTier: number[] = [];
  internals.pumpAmm = {
    buildBuyQuoted: async (_pool: string, _user: unknown, _lamports: bigint, slippagePct: number) => {
      quoted.push(slippagePct);
      pendingTier.push(slippagePct);
      return {
        ixs: [],
        baseReserve: opts.reserves?.baseReserve ?? 10n ** 15n,
        quoteReserveLamports: opts.reserves?.quoteReserveLamports ?? 100n * 10n ** 9n,
      };
    },
  };
  internals.blockhashes = { get: async () => BLOCKHASH, invalidate: () => {} };
  internals.broadcaster = {
    simulateOnly: async () => {
      const tier = pendingTier.shift()!;
      simulated.push(tier);
      return { err: failTiers.has(tier) ? EXCEEDED_SLIPPAGE : null, logs: [] };
    },
    broadcast: async (_bytes: Uint8Array, _label: string, o?: Record<string, unknown>) => {
      broadcasts.push({
        skipSimulation: o?.skipSimulation as boolean | undefined,
        confirmTimeoutMs: o?.confirmTimeoutMs as number | undefined,
      });
      return sent('buy-sig');
    },
  };

  return { exec, quoted, simulated, broadcasts, config };
}

describe('parallel buy simulate', () => {
  it('simulates every tier but sends exactly once', async () => {
    const { exec, quoted, simulated, broadcasts } = executorWith({});

    const result = await exec.buyAndConfirm('pool', 'mint', 0.1);

    expect(quoted.sort()).toEqual([5, 8]);
    expect(simulated.sort()).toEqual([5, 8]);
    // The whole point: two simulates, ONE send.
    expect(broadcasts).toHaveLength(1);
    expect(result.sent).toBe(true);
  });

  it('sends the tightest tier when it simulates clean', async () => {
    const { exec, broadcasts } = executorWith({ failTiers: [] });

    await exec.buyAndConfirm('pool', 'mint', 0.1);

    expect(broadcasts).toHaveLength(1);
    // Already simulated those exact bytes, so the send skips the redundant hop.
    expect(broadcasts[0]!.skipSimulation).toBe(true);
  });

  it('falls through to the next tier when the tight one fails slippage', async () => {
    const { exec, simulated, broadcasts } = executorWith({ failTiers: [5] });

    const result = await exec.buyAndConfirm('pool', 'mint', 0.1);

    // Serially this cost an extra state read + assemble + simulate.
    expect(simulated.sort()).toEqual([5, 8]);
    expect(broadcasts).toHaveLength(1);
    expect(result.sent).toBe(true);
  });

  it('reports the tightest tier simErr and sends nothing when all tiers fail', async () => {
    const { exec, broadcasts } = executorWith({ failTiers: [5, 8] });

    const result = await exec.buyAndConfirm('pool', 'mint', 0.1);

    expect(broadcasts).toHaveLength(0);
    expect(result.sent).toBe(false);
    expect(result.simulated).toBe(true);
    // describeBuyFailure and the slippage ladder both key off simErr.
    expect(result.simErr).toEqual(EXCEEDED_SLIPPAGE);
  });

  it('applies the buy confirmation budget, not the 12s broadcaster default', async () => {
    const { exec, broadcasts, config } = executorWith({});

    await exec.buyAndConfirm('pool', 'mint', 0.1);

    expect(config.execution.buyConfirmTimeoutMs).toBe(4_000);
    expect(broadcasts[0]!.confirmTimeoutMs).toBe(4_000);
  });

  it('still honours the entry move gate, and vetoes the whole entry', async () => {
    // The gate compares the verdict snapshot against the quote's reserves, so it
    // is a property of the entry, not of any one tier.
    const { exec, broadcasts } = executorWith({
      reserves: { baseReserve: 10n ** 15n / 2n, quoteReserveLamports: 100n * 10n ** 9n },
    });
    (exec as unknown as { config: { entry: { maxEntryMovePct: number } } }).config.entry.maxEntryMovePct = 20;

    await expect(
      exec.buyAndConfirm('pool', 'mint', 0.1, { baseReserve: 10n ** 15n, quoteReserveLamports: 100n * 10n ** 9n }),
    ).rejects.toBeInstanceOf(EntryMoveExceeded);
    expect(broadcasts).toHaveLength(0);
  });

  it('records entryMovePct on the result exactly as the serial path did', async () => {
    const { exec } = executorWith({
      reserves: { baseReserve: 10n ** 15n, quoteReserveLamports: 110n * 10n ** 9n },
    });

    const result = await exec.buyAndConfirm('pool', 'mint', 0.1, {
      baseReserve: 10n ** 15n,
      quoteReserveLamports: 100n * 10n ** 9n,
    });

    expect(result.entryMovePct).toBeCloseTo(10, 5);
  });

  it('is bypassed with a single tier, and when disabled by config', async () => {
    const single = executorWith({ retryTiers: [] });
    await single.exec.buyAndConfirm('pool', 'mint', 0.1);
    // One tier means there is nothing to parallelise — the serial path runs.
    expect(single.quoted).toEqual([5]);

    const off = executorWith({ parallel: false });
    await off.exec.buyAndConfirm('pool', 'mint', 0.1);
    expect(off.quoted).toEqual([5]);
    // The serial path simulates inside broadcast(), not via simulateOnly.
    expect(off.simulated).toEqual([]);
    expect(off.broadcasts).toHaveLength(1);
    expect(off.broadcasts[0]!.skipSimulation).toBeUndefined();
  });
});

describe('fee plan cache', () => {
  it('fetches once inside the TTL and again past it', async () => {
    let calls = 0;
    const rpc = {
      getPriorityFeeEstimate: async () => {
        calls++;
        return 300_000;
      },
      getRecentPrioritizationFees: async () => {
        calls++;
        return 300_000;
      },
    } as unknown as RpcClient;
    const config = ConfigSchema.parse({
      mode: 'dry-run',
      rpc: { primaryHttp: 'http://127.0.0.1:1' },
      fees: { planCacheMs: 50_000 },
    });
    const exec = new Executor({ config, rpc, httpUrl: 'http://127.0.0.1:1' });
    const feePlan = (exec as unknown as { feePlan(): Promise<unknown> }).feePlan.bind(exec);

    await feePlan();
    await feePlan();
    await feePlan();
    expect(calls).toBe(1);
  });

  it('a zero TTL disables the cache', async () => {
    let calls = 0;
    const rpc = {
      getPriorityFeeEstimate: async () => {
        calls++;
        return 300_000;
      },
    } as unknown as RpcClient;
    const config = ConfigSchema.parse({
      mode: 'dry-run',
      rpc: { primaryHttp: 'http://127.0.0.1:1' },
      fees: { planCacheMs: 0 },
    });
    const exec = new Executor({ config, rpc, httpUrl: 'http://127.0.0.1:1' });
    const feePlan = (exec as unknown as { feePlan(): Promise<unknown> }).feePlan.bind(exec);

    await feePlan();
    await feePlan();
    expect(calls).toBe(2);
  });
});
