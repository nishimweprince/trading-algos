import { describe, it, expect } from 'vitest';
import { computeEarlyFlow, MomentumSampler } from '../src/enrichment/momentum.ts';
import { LAMPORTS_PER_SOL } from '../src/core/constants.ts';
import type { RpcClient } from '../src/core/rpc.ts';

/** Minimal SPL token-account buffer with `amount` (u64 LE) at offset 64. */
function tokenAccountBase64(amountLamports: bigint): string {
  const buf = Buffer.alloc(72);
  buf.writeBigUInt64LE(amountLamports, 64);
  return buf.toString('base64');
}

const sol = (n: number): bigint => BigInt(n) * BigInt(LAMPORTS_PER_SOL);

describe('computeEarlyFlow', () => {
  it('reports net SOL inflow and rate for a buying pool', () => {
    const flow = computeEarlyFlow(sol(30), sol(50), 4000);
    expect(flow.netInflowSol).toBeCloseTo(20, 9);
    expect(flow.inflowRateSolPerSec).toBeCloseTo(5, 9); // 20 SOL over 4s
    expect(flow.quoteReserveStartLamports).toBe(sol(30));
    expect(flow.quoteReserveEndLamports).toBe(sol(50));
  });

  it('reports negative inflow when the pool bleeds SOL (net sells)', () => {
    const flow = computeEarlyFlow(sol(50), sol(42), 4000);
    expect(flow.netInflowSol).toBeCloseTo(-8, 9);
    expect(flow.inflowRateSolPerSec).toBeLessThan(0);
  });

  it('never divides by zero on a zero-length window', () => {
    const flow = computeEarlyFlow(sol(30), sol(40), 0);
    expect(flow.inflowRateSolPerSec).toBe(0);
    expect(flow.netInflowSol).toBeCloseTo(10, 9);
  });
});

/** Fake RPC returning a preset vault balance, recording the queried vault. */
function fakeRpc(endLamports: bigint | null): { rpc: RpcClient; queried: string[] } {
  const queried: string[] = [];
  const rpc = {
    getAccountInfoBase64: async (pubkey: string) => {
      queried.push(pubkey);
      return endLamports === null ? null : { data: tokenAccountBase64(endLamports), owner: 'o', lamports: 0 };
    },
  } as unknown as RpcClient;
  return { rpc, queried };
}

describe('MomentumSampler', () => {
  it('waits the window, re-reads the quote vault, and derives the flow', async () => {
    const { rpc, queried } = fakeRpc(sol(45));
    const slept: number[] = [];
    let clock = 1000;
    const sampler = new MomentumSampler({
      rpc,
      now: () => clock,
      sleep: async (ms) => { slept.push(ms); clock += ms; },
    });

    const flow = await sampler.sample('quoteVault', sol(30), 4000);
    expect(slept).toEqual([4000]);
    expect(queried).toEqual(['quoteVault']);
    expect(flow?.netInflowSol).toBeCloseTo(15, 9);
    expect(flow?.windowMs).toBe(4000); // observed elapsed == the injected sleep
    expect(flow?.inflowRateSolPerSec).toBeCloseTo(3.75, 9);
  });

  it('returns null when sampling is disabled (windowMs 0)', async () => {
    const { rpc, queried } = fakeRpc(sol(45));
    const sampler = new MomentumSampler({ rpc });
    expect(await sampler.sample('quoteVault', sol(30), 0)).toBeNull();
    expect(queried).toEqual([]); // no RPC read at all
  });

  it('returns null when the quote vault cannot be read', async () => {
    const { rpc } = fakeRpc(null);
    const sampler = new MomentumSampler({ rpc, sleep: async () => {} });
    expect(await sampler.sample('quoteVault', sol(30), 4000)).toBeNull();
  });
});
