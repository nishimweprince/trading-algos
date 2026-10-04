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

  it('prefers the Helius fee estimate when available', async () => {
    const cfg = ConfigSchema.parse({
      mode: 'paper',
      fees: { priorityFloorMicroLamports: 100 },
    });
    const mock = {
      getPriorityFeeEstimate: async () => 5_000,
      getRecentPrioritizationFees: async () => [1, 2, 3, 4],
    } as unknown as RpcClient;
    const plan = await buildFeePlan(mock, cfg);
    expect(plan.priorityMicroLamports).toBe(5_000);
  });

  it('falls back to p75 when the Helius estimate is unavailable', async () => {
    const cfg = ConfigSchema.parse({
      mode: 'paper',
      fees: { priorityFloorMicroLamports: 0 },
    });
    const mock = {
      getPriorityFeeEstimate: async () => null,
      getRecentPrioritizationFees: async () => [1, 2, 3, 4],
    } as unknown as RpcClient;
    const plan = await buildFeePlan(mock, cfg);
    expect(plan.priorityMicroLamports).toBe(3);
  });

  it('skips the Helius estimate when disabled', async () => {
    const cfg = ConfigSchema.parse({
      mode: 'paper',
      fees: { priorityFloorMicroLamports: 0, useHeliusFeeEstimate: false },
    });
    let called = false;
    const mock = {
      getPriorityFeeEstimate: async () => { called = true; return 9_999; },
      getRecentPrioritizationFees: async () => [1, 2, 3, 4],
    } as unknown as RpcClient;
    const plan = await buildFeePlan(mock, cfg);
    expect(called).toBe(false);
    expect(plan.priorityMicroLamports).toBe(3);
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

describe('landing bid (2026-10-04): Sender Max buys, staked-only exits, scoped priority', () => {
  afterEach(() => vi.unstubAllGlobals());

  it('tips buys at the Jito landed p75 x 1.1 (clamped) and exits at the swqos minimum', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify([{ landed_tips_75th_percentile: 0.0015 }]))));
    const cfg = ConfigSchema.parse({
      mode: 'paper',
      fees: { useHeliusFeeEstimate: false },
      heliusSender: { enabled: true, swqosOnly: false, exitSwqosOnly: true, minTipLamports: 1_000_000, tipCapLamports: 3_000_000, tipPercentile: 75, tipBufferPct: 10 },
    });
    const plan = await buildFeePlan(rpc([1]), cfg);
    expect(plan.jitoTipLamports).toBe(1_650_000);
    expect(plan.exitTipLamports).toBe(5_000);
  });

  it('asks Helius for the configured level, scoped to PumpSwap + WSOL', async () => {
    const getPriorityFeeEstimate = vi.fn(async () => 900_000);
    const cfg = ConfigSchema.parse({ mode: 'paper', fees: { priorityLevel: 'veryHigh', priorityFloorMicroLamports: 500_000 } });
    const plan = await buildFeePlan({ getPriorityFeeEstimate, getRecentPrioritizationFees: async () => [] } as unknown as RpcClient, cfg);
    expect(plan.priorityMicroLamports).toBe(900_000);
    expect(getPriorityFeeEstimate).toHaveBeenCalledWith(['pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA', 'So11111111111111111111111111111111111111112'], 'veryHigh');
  });

  it('falls back to the configured percentile of scoped recent fees', async () => {
    const getRecentPrioritizationFees = vi.fn(async (_accounts?: string[]) => Array.from({ length: 100 }, (_, i) => (i + 1) * 10_000));
    const cfg = ConfigSchema.parse({ mode: 'paper', fees: { useHeliusFeeEstimate: false, priorityPercentile: 90, priorityFloorMicroLamports: 0 } });
    const plan = await buildFeePlan({ getRecentPrioritizationFees } as unknown as RpcClient, cfg);
    expect(plan.priorityMicroLamports).toBeGreaterThanOrEqual(900_000);
    expect(getRecentPrioritizationFees.mock.calls[0]![0]).toHaveLength(2);
  });
});
