import { afterEach, describe, expect, it, vi } from 'vitest';
import { buildFeePlan } from '../src/executor/fees.ts';
import { ConfigSchema } from '../src/config/schema.ts';
import type { RpcClient } from '../src/core/rpc.ts';

const rpc = (fees: number[]) => ({
  getRecentPrioritizationFees: async () => fees,
}) as unknown as RpcClient;

describe('buildFeePlan', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('uses p75 priority fees when above the floor, and clamps dynamic Jito tips', async () => {
    vi.stubGlobal('fetch', vi.fn(async () =>
      new Response(JSON.stringify([{ ema_landed_tips_50th_percentile: 0.5 }])),
    ));
    const cfg = ConfigSchema.parse({
      mode: 'paper',
      fees: { priorityFloorMicroLamports: 0 }, // let p75 through to assert it is used
      jito: {
        blockEngineUrl: 'https://mainnet.block-engine.jito.wtf',
        tipCapLamports: 2_000,
        minTipLamports: 1_000,
        fallbackTipLamports: 1_500,
      },
    });

    const plan = await buildFeePlan(rpc([1, 2, 3, 4]), cfg);
    expect(plan.priorityMicroLamports).toBe(3);
    expect(plan.jitoTipLamports).toBe(2_000);
  });

  it('never bids below the configured priority floor', async () => {
    const cfg = ConfigSchema.parse({ mode: 'paper' }); // default floor 250_000
    // p75 of [1,2,3,4] is 3, well below the floor → floor wins.
    const plan = await buildFeePlan(rpc([1, 2, 3, 4]), cfg);
    expect(plan.priorityMicroLamports).toBe(250_000);
  });

  it('clamps priority to the configured cap', async () => {
    const cfg = ConfigSchema.parse({
      mode: 'paper',
      fees: { priorityFloorMicroLamports: 0, priorityCapMicroLamports: 1_000 },
    });
    const plan = await buildFeePlan(rpc([5_000, 6_000, 7_000]), cfg);
    expect(plan.priorityMicroLamports).toBe(1_000);
  });

  it('falls back to the floor when tip-floor fetch fails', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response('nope', { status: 500 })));
    const cfg = ConfigSchema.parse({
      mode: 'paper',
      jito: {
        blockEngineUrl: 'https://mainnet.block-engine.jito.wtf',
        tipCapLamports: 20_000,
        minTipLamports: 1_000,
        fallbackTipLamports: 12_345,
      },
    });

    const plan = await buildFeePlan(rpc([]), cfg);
    expect(plan.priorityMicroLamports).toBe(250_000); // default floor
    expect(plan.jitoTipLamports).toBe(12_345);
  });
});
