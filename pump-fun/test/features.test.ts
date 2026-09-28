import { describe, it, expect } from 'vitest';
import { parseSwaps, flowStats, fetchSwaps } from '../src/enrichment/txFlow.ts';
import type { ParsedTx, SignatureInfo } from '../src/core/rpc.ts';
import { washRatioOf, curveFeatures } from '../src/enrichment/features/curve.ts';
import { FeatureEngine } from '../src/enrichment/features/index.ts';
import { imageFingerprint, nameFingerprint, copycatFeatures } from '../src/enrichment/features/copycat.ts';
import { funderFromTx, creatorCluster } from '../src/enrichment/features/cluster.ts';
import { sniperFeatures } from '../src/enrichment/features/snipers.ts';
import { checkManipulation } from '../src/guardrails/checks/manipulation.ts';
import { openDb } from '../src/persistence/db.ts';
import { Repositories } from '../src/persistence/repositories.ts';
import { ConfigSchema } from '../src/config/schema.ts';
import { PUMP_FUN_MIGRATION_AUTHORITY, WSOL_MINT } from '../src/core/constants.ts';
import type { Candidate } from '../src/enrichment/types.ts';
import type { CheckContext } from '../src/guardrails/engine.ts';

const MINT = 'TokenMint111pump';
const POOL = 'Pool111';
const VAULT = 'QuoteVault111';

/** A jsonParsed tx where `trader` moves `tokens` of MINT against the pool. */
function swapTx(opts: {
  sig: string;
  slot?: number;
  blockTime?: number;
  trader: string;
  tokensPre: bigint;
  tokensPost: bigint;
  vaultPre?: bigint;
  vaultPost?: bigint;
  lamportsPre?: number;
  lamportsPost?: number;
}): ParsedTx {
  const keys = [
    { pubkey: opts.trader, signer: true },
    { pubkey: POOL, signer: false },
    { pubkey: VAULT, signer: false },
  ];
  return {
    slot: opts.slot ?? 100,
    blockTime: opts.blockTime ?? 1_000,
    meta: {
      err: null,
      preBalances: [opts.lamportsPre ?? 10e9, 0, 0],
      postBalances: [opts.lamportsPost ?? 10e9, 0, 0],
      preTokenBalances: [
        { accountIndex: 3, mint: MINT, owner: opts.trader, uiTokenAmount: { amount: opts.tokensPre.toString(), decimals: 6 } },
        { accountIndex: 2, mint: WSOL_MINT, owner: POOL, uiTokenAmount: { amount: (opts.vaultPre ?? 0n).toString(), decimals: 9 } },
      ],
      postTokenBalances: [
        { accountIndex: 3, mint: MINT, owner: opts.trader, uiTokenAmount: { amount: opts.tokensPost.toString(), decimals: 6 } },
        { accountIndex: 2, mint: WSOL_MINT, owner: POOL, uiTokenAmount: { amount: (opts.vaultPost ?? 0n).toString(), decimals: 9 } },
      ],
    },
    transaction: { signatures: [opts.sig], message: { accountKeys: keys } },
  };
}

describe('txFlow', () => {
  it('parses a buy from token-balance deltas with SOL from the WSOL vault', () => {
    const tx = swapTx({ sig: 's1', trader: 'Alice', tokensPre: 0n, tokensPost: 5_000n, vaultPre: 80_000_000_000n, vaultPost: 81_500_000_000n });
    const [s] = parseSwaps(tx, MINT, new Set([POOL]), { quoteVault: VAULT });
    expect(s).toMatchObject({ trader: 'Alice', side: 'buy', tokenAmount: 5_000n, feePayer: 'Alice' });
    expect(s!.sol).toBeCloseTo(1.5, 9);
  });

  it('parses a sell and ignores the venue owner', () => {
    const tx = swapTx({ sig: 's2', trader: 'Bob', tokensPre: 9_000n, tokensPost: 1_000n, lamportsPre: 1e9, lamportsPost: 3e9 });
    const swaps = parseSwaps(tx, MINT, new Set([POOL]));
    expect(swaps).toHaveLength(1);
    expect(swaps[0]).toMatchObject({ side: 'sell', tokenAmount: 8_000n });
    expect(swaps[0]!.sol).toBeCloseTo(2, 9);
  });

  it('drops the migration tx — the curve funding the pool is liquidity, not an 85 SOL sell', () => {
    // v1 read the bonding curve's token outflow as a trader selling, priced at
    // the whole WSOL vault: maxSellSol = 85.0054 on every graduation (2026-09-27).
    const tx = swapTx({ sig: 'mig', trader: 'Curve', tokensPre: 206_900_000n, tokensPost: 0n, vaultPre: 0n, vaultPost: 85_005_360_973n });
    tx.transaction.message.accountKeys.push({ pubkey: PUMP_FUN_MIGRATION_AUTHORITY, signer: true });
    expect(parseSwaps(tx, MINT, new Set([POOL]), { quoteVault: VAULT })).toEqual([]);
    expect(flowStats(parseSwaps(tx, MINT, new Set([POOL]), { quoteVault: VAULT })).maxSellSol).toBe(0);
  });

  it('flow stats count unique buyers and the largest sell', () => {
    const swaps = [
      ...parseSwaps(swapTx({ sig: 'a', trader: 'A', tokensPre: 0n, tokensPost: 1n, lamportsPre: 3e9, lamportsPost: 2e9 }), MINT, new Set()),
      ...parseSwaps(swapTx({ sig: 'b', trader: 'A', tokensPre: 0n, tokensPost: 1n, lamportsPre: 3e9, lamportsPost: 2e9 }), MINT, new Set()),
      ...parseSwaps(swapTx({ sig: 'c', trader: 'B', tokensPre: 5n, tokensPost: 0n, lamportsPre: 0, lamportsPost: 4e9 }), MINT, new Set()),
    ];
    expect(flowStats(swaps)).toMatchObject({ buyCount: 2, sellCount: 1, uniqueBuyers: 1, maxSellSol: 4 });
  });

  it('wash ratio = share of volume by wallets on both sides', () => {
    const swaps = [
      ...parseSwaps(swapTx({ sig: '1', trader: 'W', tokensPre: 0n, tokensPost: 100n }), MINT, new Set()),
      ...parseSwaps(swapTx({ sig: '2', trader: 'W', tokensPre: 100n, tokensPost: 0n }), MINT, new Set()),
      ...parseSwaps(swapTx({ sig: '3', trader: 'H', tokensPre: 0n, tokensPost: 200n }), MINT, new Set()),
    ];
    expect(washRatioOf(swaps)).toBeCloseTo(0.5, 9);
  });

  it('fetchSwaps stops at the deadline and reports incomplete', async () => {
    let t = 0;
    const rpc = {
      getSignaturesForAddress: async () => Array.from({ length: 30 }, (_, i) => ({ signature: `s${i}`, slot: 1, blockTime: 1, err: null })) as SignatureInfo[],
      getParsedTransaction: async (sig: string) => {
        t += 10;
        return swapTx({ sig, trader: 'T', tokensPre: 0n, tokensPost: 1n });
      },
    };
    const r = await fetchSwaps(rpc, POOL, MINT, new Set([POOL]), { maxTx: 30, deadlineMs: 150, now: () => t });
    expect(r.complete).toBe(false);
    expect(r.swaps.length).toBeLessThan(30);
  });
});

describe('curve features', () => {
  it('measures the creation-slot bundle share when the scan reaches the first tx', async () => {
    const supply = 1_000_000_000n * 1_000_000n;
    const sigs: SignatureInfo[] = [
      { signature: 'late', slot: 500, blockTime: 2, err: null },
      { signature: 'b2', slot: 100, blockTime: 1, err: null },
      { signature: 'b1', slot: 100, blockTime: 1, err: null },
    ];
    const txs: Record<string, ParsedTx> = {
      late: swapTx({ sig: 'late', slot: 500, trader: 'X', tokensPre: 0n, tokensPost: 1n }),
      b1: swapTx({ sig: 'b1', slot: 100, trader: 'Dev', tokensPre: 0n, tokensPost: 100_000_000n * 1_000_000n }),
      b2: swapTx({ sig: 'b2', slot: 100, trader: 'Bundler', tokensPre: 0n, tokensPost: 150_000_000n * 1_000_000n }),
    };
    const rpc = { getSignaturesForAddress: async () => sigs, getParsedTransaction: async (s: string) => txs[s]! };
    const f = await curveFeatures(rpc, 'Curve', MINT, supply, { maxPages: 3, maxCreationTx: 20, washSampleTx: 3, deadlineMs: Infinity });
    expect(f.creationSlot).toBe(100);
    expect(f.creationSlotBuyers).toBe(2);
    expect(f.bundleSharePct).toBeCloseTo(25, 9);
  });

  it('reports an unknown creation slot when history is longer than the cap', async () => {
    const page = Array.from({ length: 1000 }, (_, i) => ({ signature: `s${i}`, slot: 10_000 - i, blockTime: 1, err: null }));
    const rpc = { getSignaturesForAddress: async () => page, getParsedTransaction: async () => null };
    const f = await curveFeatures(rpc, 'Curve', MINT, 1n, { maxPages: 1, maxCreationTx: 5, washSampleTx: 0, deadlineMs: Infinity });
    expect(f.creationSlot).toBeNull();
    expect(f.bundleSharePct).toBeNull();
    expect(f.oldestSlotScanned).toBe(10_000 - 999);
  });

  it('requests growing page sizes: 100 first, then 1000', async () => {
    const limits: number[] = [];
    let call = 0;
    const rpc = {
      getSignaturesForAddress: async (_addr: string, opts?: { limit?: number }) => {
        const limit = opts?.limit ?? -1;
        limits.push(limit);
        call += 1;
        if (call >= 4) return [];
        return Array.from({ length: limit }, (_, i) => ({ signature: `p${call}s${i}`, slot: 1_000_000 - call * 10_000 - i, blockTime: 1, err: null }));
      },
      getParsedTransaction: async () => null,
    };
    await curveFeatures(rpc, 'Curve', MINT, 1n, { maxPages: 4, maxCreationTx: 5, washSampleTx: 0, deadlineMs: Infinity });
    expect(limits).toEqual([100, 1000, 1000, 1000]);
  });

  it('fewer than 100 sigs on page 0 sets the creation slot', async () => {
    const sigs: SignatureInfo[] = Array.from({ length: 27 }, (_, i) => ({ signature: `s${i}`, slot: 200 - i, blockTime: 1, err: null }));
    const rpc = { getSignaturesForAddress: async () => sigs, getParsedTransaction: async () => null };
    const f = await curveFeatures(rpc, 'Curve', MINT, 1n, { maxPages: 4, maxCreationTx: 5, washSampleTx: 0, deadlineMs: Infinity });
    expect(f.creationSlot).toBe(200 - 26);
    expect(f.txScanned).toBe(27);
  });

  it('publishes the creation slot to `progress` before the tx parsing', async () => {
    const sigs: SignatureInfo[] = [
      { signature: 'late', slot: 500, blockTime: 2, err: null },
      { signature: 'first', slot: 100, blockTime: 1, err: null },
    ];
    const progress = { creationSlot: null as number | null, txScanned: 0, oldestSlotScanned: null as number | null };
    let parseCalls = 0;
    const rpc = {
      getSignaturesForAddress: async () => sigs,
      getParsedTransaction: async () => {
        parseCalls += 1;
        // Paging is done by the time any tx is parsed.
        expect(progress).toEqual({ creationSlot: 100, txScanned: 2, oldestSlotScanned: 100 });
        return null;
      },
    };
    await curveFeatures(rpc, 'Curve', MINT, 1n, { maxPages: 3, maxCreationTx: 5, washSampleTx: 2, deadlineMs: Infinity, progress });
    expect(parseCalls).toBeGreaterThan(0);
  });
});

describe('FeatureEngine curve budget', () => {
  // A real base58 mint so the bonding-curve PDA derives.
  const REAL_MINT = 'ZYNgmBkofey8GV4f1E3amu358KjdLhJKnXV57Pzpump';
  const engine = (rpc: ConstructorParameters<typeof FeatureEngine>[0]['rpc']) =>
    new FeatureEngine({
      rpc,
      repos: new Repositories(openDb({ path: ':memory:', memory: true })),
      config: ConfigSchema.parse({
        guardrails: {
          features: {
            enabled: true,
            budgetMs: 50,
            cluster: { enabled: false },
            copycat: { enabled: false },
            curve: { enabled: true, maxPages: 3, maxCreationTx: 5, washSampleTx: 5 },
          },
        },
      }).guardrails.features,
    });
  const candidate = (slot: number) =>
    ({
      graduation: { mint: REAL_MINT, slot, feedSource: 'helius-ws', detectedAtNs: 0n },
      enrichment: { unknowns: [], elapsedMs: 0, mintInfo: { supply: 1_000_000n, decimals: 6 } },
    }) as unknown as Candidate;

  it('keeps the scanned slots when tx parsing overruns the budget (2026-09-28: curve missing on old mints)', async () => {
    const page = Array.from({ length: 1000 }, (_, i) => ({ signature: `s${i}`, slot: 50_000 - i, blockTime: 1, err: null }));
    const f = await engine({
      getSignaturesForAddress: async () => page,
      getParsedTransaction: () => new Promise(() => {}), // never resolves
    }).compute(candidate(51_000));
    expect(f.missing).toContain('curve');
    expect(f.curve).toMatchObject({ partial: true, creationSlot: null, txScanned: 3000, oldestSlotScanned: 50_000 - 999, washRatio: null });
    // A lower bound is not a time-to-graduate.
    expect(f.timeToGraduateMs).toBeNull();
  });

  it('keeps page-0 slots as a partial lower bound when page 1 throws', async () => {
    const page0 = Array.from({ length: 100 }, (_, i) => ({ signature: `s${i}`, slot: 60_000 - i, blockTime: 1, err: null }));
    let calls = 0;
    const f = await engine({
      getSignaturesForAddress: async () => {
        calls += 1;
        if (calls > 1) throw new Error('page 1 timeout');
        return page0;
      },
      getParsedTransaction: async () => null,
    }).compute(candidate(61_000));
    expect(f.missing).toContain('curve');
    expect(f.curve).toMatchObject({ partial: true, creationSlot: null, txScanned: 100, oldestSlotScanned: 60_000 - 99 });
  });

  it('derives time to graduate from a creation slot found before the overrun', async () => {
    const sigs: SignatureInfo[] = [
      { signature: 'late', slot: 1_500, blockTime: 2, err: null },
      { signature: 'first', slot: 1_000, blockTime: 1, err: null },
    ];
    const f = await engine({
      getSignaturesForAddress: async () => sigs,
      getParsedTransaction: () => new Promise(() => {}),
    }).compute(candidate(2_000));
    expect(f.curve).toMatchObject({ partial: true, creationSlot: 1_000 });
    expect(f.timeToGraduateMs).toBe(1_000 * 400);
  });
});

describe('copycat', () => {
  it('fingerprints names and IPFS CIDs', () => {
    expect(nameFingerprint('Dog Wif Hat!', '$WIF')).toBe('dogwifhat|wif');
    const cid = 'QmYwAPJzv5CZsnA625s3Xf2nemtYgPpHdWEz79ojWnPbdG';
    expect(imageFingerprint(`https://ipfs.io/ipfs/${cid}`)).toBe(`cid:${cid}`);
    expect(imageFingerprint(`https://gateway.x/ipfs/${cid}?a=1`)).toBe(`cid:${cid}`);
  });

  it('flags a later mint that reuses a name or image', () => {
    const repos = new Repositories(openDb({ path: ':memory:', memory: true }));
    const meta = { name: 'Moon', symbol: 'MOON', links: { image: 'https://ipfs.io/ipfs/QmYwAPJzv5CZsnA625s3Xf2nemtYgPpHdWEz79ojWnPbdG' } };
    expect(copycatFeatures(repos, 'M1', meta).isCopycat).toBe(false);
    expect(copycatFeatures(repos, 'M2', meta)).toMatchObject({ isCopycat: true, nameMatches: 1, imageMatches: 1 });
  });
});

describe('creator cluster', () => {
  it('attributes the funder as the fee payer of the first inbound transfer', () => {
    const tx: ParsedTx = {
      slot: 1, blockTime: 1,
      meta: { err: null, preBalances: [5e9, 0], postBalances: [4e9, 1e9] },
      transaction: { signatures: ['x'], message: { accountKeys: [{ pubkey: 'Funder', signer: true }, { pubkey: 'Creator', signer: false }] } },
    };
    expect(funderFromTx(tx, 'Creator')).toBe('Funder');
  });

  it('clusters creators sharing a funder and never through a hub', async () => {
    const repos = new Repositories(openDb({ path: ':memory:', memory: true }));
    const fundedBy: Record<string, string> = { C1: 'Op', C2: 'Op', Op: 'OpRoot' };
    const rpc = {
      getSignaturesForAddress: async (addr: string) =>
        addr === 'Hub'
          ? Array.from({ length: 50 }, (_, i) => ({ signature: `h${i}`, slot: 1, blockTime: 1, err: null }))
          : [{ signature: `first-${addr}`, slot: 1, blockTime: 1, err: null }],
      getParsedTransaction: async (sig: string): Promise<ParsedTx> => {
        const w = sig.replace('first-', '');
        const f = fundedBy[w] ?? 'Hub';
        return {
          slot: 1, blockTime: 1,
          meta: { err: null, preBalances: [5e9, 0], postBalances: [4e9, 1e9] },
          transaction: { signatures: [sig], message: { accountKeys: [{ pubkey: f, signer: true }, { pubkey: w, signer: false }] } },
        };
      },
    };
    for (const [mint, creator] of [['m1', 'C1'], ['m2', 'C2'], ['m3', 'C2']] as const) {
      repos.recordLaunch({ mint, feedSource: 'pumpportal', receivedAtNs: 0n, creator });
    }
    await creatorCluster(rpc, repos, 'C1', { hops: 2, maxSigs: 50 });
    const c2 = await creatorCluster(rpc, repos, 'C2', { hops: 2, maxSigs: 50 });
    expect(c2.root).toBe('OpRoot');
    expect(c2.launches7d).toBe(3);
    expect(c2.wallets).toBe(2);

    // Solo is funded by an exchange-like hub: stays its own cluster.
    fundedBy.Solo = 'Hub';
    repos.recordLaunch({ mint: 'm4', feedSource: 'pumpportal', receivedAtNs: 0n, creator: 'Solo' });
    const solo = await creatorCluster(rpc, repos, 'Solo', { hops: 2, maxSigs: 50 });
    expect(solo).toMatchObject({ root: 'Solo', hubFunder: true, launches7d: 1 });
  });
});

describe('snipers', () => {
  it('scores early buy volume from wallets seen sniping other coins', () => {
    const repos = new Repositories(openDb({ path: ':memory:', memory: true }));
    for (const m of ['a', 'b', 'c']) repos.recordSniperObservations(m, ['Sniper']);
    const swaps = [
      ...parseSwaps(swapTx({ sig: '1', trader: 'Sniper', blockTime: 100, tokensPre: 0n, tokensPost: 1n, lamportsPre: 5e9, lamportsPost: 2e9 }), MINT, new Set()),
      ...parseSwaps(swapTx({ sig: '2', trader: 'Retail', blockTime: 101, tokensPre: 0n, tokensPost: 1n, lamportsPre: 5e9, lamportsPost: 4e9 }), MINT, new Set()),
      ...parseSwaps(swapTx({ sig: '3', trader: 'Late', blockTime: 200, tokensPre: 0n, tokensPost: 1n, lamportsPre: 5e9, lamportsPost: 4e9 }), MINT, new Set()),
    ];
    const f = sniperFeatures(repos, MINT, swaps, { windowSec: 10, minCoins: 3 });
    expect(f).toMatchObject({ earlyBuyers: 2, knownSnipers: 1 });
    expect(f.sniperBuyShare).toBeCloseTo(0.75, 9);
  });
});

describe('H13 manipulation screens', () => {
  const ctx = (features: Candidate['enrichment']['features'], cfg: Record<string, unknown> = {}): CheckContext => ({
    candidate: { graduation: { mint: MINT, venue: 'pumpswap', poolAddress: '', slot: 1, feedSource: 'pumpportal', receivedAtNs: 0n }, enrichment: { unknowns: [], elapsedMs: 0, ...(features ? { features } : {}) } },
    config: ConfigSchema.parse({ guardrails: { creatorMaxLaunches7d: 3, features: { enabled: true, ...cfg } } }),
    repos: new Repositories(openDb({ path: ':memory:', memory: true })),
    mode: 'dry-run',
    walletSol: 0,
  });

  it('vetoes a funding cluster over creatorMaxLaunches7d', () => {
    const r = checkManipulation(ctx({ cluster: { funder: 'F', root: 'Root', launches7d: 9, wallets: 4, hubFunder: false } }));
    expect(r).toMatchObject({ status: 'fail', reason: 'creator_cluster' });
  });

  it('is advisory for thresholds that are not configured', () => {
    const f = { curve: { creationSlot: 1, txScanned: 10, bundleSharePct: 60, creationSlotBuyers: 9, washRatio: 0.9 } };
    expect(checkManipulation(ctx(f)).status).toBe('pass');
    expect(checkManipulation(ctx(f, { maxBundleSharePct: 40 }))).toMatchObject({ status: 'fail', reason: 'bundled_launch' });
  });
});
