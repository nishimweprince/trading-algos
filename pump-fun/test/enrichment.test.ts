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
  // sampleMomentum runs separately from enrich() (GuardrailPipeline.screen runs
  // it concurrently with the H4 sellability probe instead of serially after
  // enrichment) — these test the bucket-picking directly.
  it('selects a deterministic per-candidate bucket with injected rng', async () => {
    const enricher = new Enricher({
      rpc: fakeRpc(),
      budgetMs: 100,
      momentumWindowMs: 1000,
      momentumWindowBucketsMs: [0, 250, 500, 750, 1000],
      rng: () => 0.61,
    });

    const result = await enricher.sampleMomentum(undefined);

    expect(result.momentumWindowMs).toBe(750);
    expect(result.missed).toBe(false); // no pool → never attempted, not a miss
  });

  it('falls back to the fixed window when buckets are empty', async () => {
    const enricher = new Enricher({
      rpc: fakeRpc(),
      budgetMs: 100,
      momentumWindowMs: 1234,
      momentumWindowBucketsMs: [],
      rng: () => 0,
    });

    const result = await enricher.sampleMomentum(undefined);

    expect(result.momentumWindowMs).toBe(1234);
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

  it('populates holders when DAS getAsset never resolves (does not mark holders unknown)', async () => {
    let largestCalls = 0;
    let gmaCalls = 0;
    const rpc = {
      ...fakeRpc(),
      getTokenSupply: async () => ({ amount: 1_000n, decimals: 6 }),
      getTokenLargestAccounts: async () => {
        largestCalls++;
        return [{ address: 'tokAcc', amount: 100n }];
      },
      getMultipleAccountsBase64: async () => {
        gmaCalls++;
        return [null];
      },
      getAsset: () => new Promise(() => {}),
    } as unknown as RpcClient;

    const candidate = await new Enricher({ rpc, budgetMs: 80 }).enrich(graduation);

    expect(largestCalls).toBe(1);
    expect(gmaCalls).toBe(1);
    expect(candidate.enrichment.holders).toBeDefined();
    expect(candidate.enrichment.holders?.supply).toBe(1_000n);
    expect(candidate.enrichment.holders?.holders).toHaveLength(1);
    expect(candidate.enrichment.unknowns).not.toContain('holders');
  });

  it('populates holders when DAS getAsset exceeds the enrichment budget', async () => {
    const rpc = {
      ...fakeRpc(),
      getTokenSupply: async () => ({ amount: 2_000n, decimals: 6 }),
      getTokenLargestAccounts: async () => [{ address: 'tokAcc', amount: 200n }],
      getMultipleAccountsBase64: async () => [null],
      getAsset: () => new Promise((resolve) => {
        setTimeout(() => resolve({ token_info: { supply: 2_000, decimals: 6 } }), 400);
      }),
    } as unknown as RpcClient;

    const started = Date.now();
    const candidate = await new Enricher({ rpc, budgetMs: 60, momentumWindowMs: 0, momentumWindowBucketsMs: [] }).enrich(graduation);
    expect(Date.now() - started).toBeLessThan(300);
    expect(candidate.enrichment.holders?.supply).toBe(2_000n);
    expect(candidate.enrichment.unknowns).not.toContain('holders');
    expect(candidate.enrichment.unknowns).toContain('metadata');
  });
});

describe('fetchHolders getTokenLargestAccounts', () => {
  it('does not emit unhandledRejection when largest-accounts rejects during the supply-hint wait', async () => {
    const { fetchHolders } = await import('../src/enrichment/holders.ts');
    const { RpcError } = await import('../src/core/rpc.ts');

    let unhandled = 0;
    const onUnhandled = () => {
      unhandled++;
    };
    process.on('unhandledRejection', onUnhandled);
    try {
      const rpc = {
        getTokenLargestAccounts: async () => {
          throw new RpcError('getTokenLargestAccounts: Invalid param: not a Token mint (-32602)');
        },
        getTokenSupply: async () => {
          await new Promise<void>((r) => setImmediate(r));
          return { amount: 1_000n, decimals: 6 };
        },
        getMultipleAccountsBase64: async () => [],
      } as unknown as RpcClient;

      await expect(fetchHolders(rpc, 'MintUnderTest', undefined, { largestRetryDelaysMs: [0] })).rejects.toBeInstanceOf(
        RpcError,
      );
      await new Promise<void>((r) => setImmediate(r));
      expect(unhandled).toBe(0);
    } finally {
      process.off('unhandledRejection', onUnhandled);
    }
  });

  it('retries -32602 not-a-Token-mint and populates holders on a later success', async () => {
    const { fetchHolders } = await import('../src/enrichment/holders.ts');
    const { RpcError } = await import('../src/core/rpc.ts');

    let largestCalls = 0;
    const rpc = {
      getTokenLargestAccounts: async () => {
        largestCalls++;
        if (largestCalls < 3) {
          throw new RpcError('getTokenLargestAccounts: Invalid param: not a Token mint (-32602)');
        }
        return [{ address: 'tokAcc', amount: 250n }];
      },
      getTokenSupply: async () => ({ amount: 1_000n, decimals: 6 }),
      getMultipleAccountsBase64: async () => [null],
    } as unknown as RpcClient;

    const snap = await fetchHolders(rpc, 'MintUnderTest', undefined, { largestRetryDelaysMs: [0, 0, 0] });
    expect(largestCalls).toBe(3);
    expect(snap.holders).toHaveLength(1);
    expect(snap.holders[0]?.amount).toBe(250n);
  });

  it('does not retry a different RPC error', async () => {
    const { fetchHolders } = await import('../src/enrichment/holders.ts');
    const { RpcError } = await import('../src/core/rpc.ts');

    let largestCalls = 0;
    const rpc = {
      getTokenLargestAccounts: async () => {
        largestCalls++;
        throw new RpcError('getTokenLargestAccounts: rate limited (-32005)', true);
      },
      getTokenSupply: async () => ({ amount: 1_000n, decimals: 6 }),
      getMultipleAccountsBase64: async () => [],
    } as unknown as RpcClient;

    await expect(fetchHolders(rpc, 'MintUnderTest', undefined, { largestRetryDelaysMs: [0, 0, 0] })).rejects.toBeInstanceOf(
      RpcError,
    );
    expect(largestCalls).toBe(1);
  });

  it('skips a not-a-Token-mint retry that cannot finish before the enrichment deadline', async () => {
    const { fetchHolders } = await import('../src/enrichment/holders.ts');
    const { RpcError } = await import('../src/core/rpc.ts');

    let largestCalls = 0;
    let t = 1_000_000;
    const rpc = {
      getTokenLargestAccounts: async () => {
        largestCalls++;
        throw new RpcError('getTokenLargestAccounts: Invalid param: not a Token mint (-32602)');
      },
      getTokenSupply: async () => ({ amount: 1_000n, decimals: 6 }),
      getMultipleAccountsBase64: async () => [],
    } as unknown as RpcClient;

    const started = Date.now();
    // Schedule: 0, 10 (fits), 1500 (would cross the 1000 ms deadline) → 2 calls, fails fast.
    await expect(
      fetchHolders(rpc, 'MintUnderTest', undefined, {
        largestRetryDelaysMs: [0, 10, 1500],
        deadlineMs: t + 1_000,
        now: () => t + (Date.now() - started),
      }),
    ).rejects.toThrow(/not a Token mint/);
    expect(largestCalls).toBe(2);
    expect(Date.now() - started).toBeLessThan(1_000);
  });
});
