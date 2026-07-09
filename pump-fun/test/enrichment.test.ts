import { describe, expect, it } from 'vitest';
import { Enricher } from '../src/enrichment/index.ts';
import type { RpcClient } from '../src/core/rpc.ts';
import type { GraduationEvent } from '../src/core/types.ts';

const graduation: GraduationEvent = {
  mint: 'MintUnderTest',
  venue: 'pumpswap',
  poolAddress: 'pool',
  slot: 1,
  feedSource: 'pumpportal',
  receivedAtNs: 0n,
};

function fakeRpc(): RpcClient {
  return {
    getAccountInfoBase64: async () => null,
    getProgramAccountsBase64: async () => [],
    getTokenSupply: async () => ({ amount: 0n, decimals: 6 }),
    getTokenLargestAccounts: async () => [],
    getMultipleAccountsBase64: async () => [],
    getAsset: async () => null,
  } as unknown as RpcClient;
}

describe('Enricher momentum windows', () => {
  it('selects a deterministic per-candidate bucket with injected rng', async () => {
    const enricher = new Enricher({
      rpc: fakeRpc(),
      budgetMs: 100,
      momentumWindowMs: 1000,
      momentumWindowBucketsMs: [0, 250, 500, 750, 1000],
      rng: () => 0.61,
    });

    const candidate = await enricher.enrich(graduation);

    expect(candidate.enrichment.momentumWindowMs).toBe(750);
  });

  it('falls back to the fixed window when buckets are empty', async () => {
    const enricher = new Enricher({
      rpc: fakeRpc(),
      budgetMs: 100,
      momentumWindowMs: 1234,
      momentumWindowBucketsMs: [],
      rng: () => 0,
    });

    const candidate = await enricher.enrich(graduation);

    expect(candidate.enrichment.momentumWindowMs).toBe(1234);
  });
});
