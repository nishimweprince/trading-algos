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

  it('uses p75 priority fees and clamps dynamic Jito tips', async () => {
    vi.stubGlobal('fetch', vi.fn(async () =>
      new Response(JSON.stringify([{ ema_landed_tips_50th_percentile: 0.5 }])),
    ));
    const cfg = ConfigSchema.parse({
      mode: 'paper',
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

  it('falls back when tip-floor fetch fails', async () => {
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
    expect(plan.priorityMicroLamports).toBe(50_000);
    expect(plan.jitoTipLamports).toBe(12_345);
  });
});
