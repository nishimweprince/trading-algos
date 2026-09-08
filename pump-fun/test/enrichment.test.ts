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

describe('DAS helpers', () => {
  it('builds a supply hint only from sane DAS values', async () => {
    const { supplyHint, parseDasFields } = await import('../src/enrichment/index.ts');
    expect(supplyHint({ token_info: { supply: 1_000_000, decimals: 6 } })).toEqual({ supply: 1_000_000n, decimals: 6 });
    expect(supplyHint(null)).toBeUndefined();
    expect(supplyHint({})).toBeUndefined();
    expect(supplyHint({ token_info: { supply: -1, decimals: 6 } })).toBeUndefined();
    expect(supplyHint({ token_info: { supply: 1.5, decimals: 6 } })).toBeUndefined();
    expect(supplyHint({ token_info: { supply: Number.MAX_SAFE_INTEGER + 1, decimals: 6 } })).toBeUndefined();
    expect(supplyHint({ token_info: { supply: 100, decimals: 19 } })).toBeUndefined();

    const fields = parseDasFields({
      token_info: { mint_authority: null, freeze_authority: 'F' },
      creators: [{ address: 'B' }, { address: 'A', verified: true }, { address: 'A' }],
    });
    expect(fields.authorities).toEqual({ mintAuthority: null, freezeAuthority: 'F' });
    expect(fields.creators).toEqual(['A', 'B']); // verified first, deduped
    expect(parseDasFields(null)).toEqual({});
    expect(parseDasFields({})).toEqual({});
  });

  it('skips getTokenSupply when the DAS hint is present', async () => {
    const { Enricher } = await import('../src/enrichment/index.ts');
    let supplyCalls = 0;
    const rpc = {
      ...fakeRpc(),
      getTokenSupply: async () => { supplyCalls++; return { amount: 5_000n, decimals: 6 }; },
      getAsset: async () => ({ token_info: { supply: 5_000, decimals: 6 } }),
    } as unknown as RpcClient;
    const candidate = await new Enricher({ rpc, budgetMs: 1000 }).enrich(graduation);
    expect(supplyCalls).toBe(0);
    expect(candidate.enrichment.holders?.supply).toBe(5_000n);
  });

  it('falls back to getTokenSupply when DAS has no supply', async () => {
    const { Enricher } = await import('../src/enrichment/index.ts');
    let supplyCalls = 0;
    const rpc = {
      ...fakeRpc(),
      getTokenSupply: async () => { supplyCalls++; return { amount: 7_000n, decimals: 6 }; },
      getAsset: async () => ({}),
    } as unknown as RpcClient;
    const candidate = await new Enricher({ rpc, budgetMs: 1000 }).enrich(graduation);
    expect(supplyCalls).toBe(1);
    expect(candidate.enrichment.holders?.supply).toBe(7_000n);
    expect(candidate.enrichment.dasAuthorities).toBeUndefined();
  });
});
