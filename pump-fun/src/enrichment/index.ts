import type { RpcClient, DasAsset } from '../core/rpc.ts';
import type { GraduationEvent } from '../core/types.ts';
import { logger } from '../core/logger.ts';
import { decodeMint } from './mint.ts';
import { fetchHolders, type SupplyHint } from './holders.ts';
import { fetchPumpSwapPool } from './pool.ts';
import { MomentumSampler, type EarlyFlow } from './momentum.ts';
import { fetchRugcheck } from './rugcheck.ts';
import { fetchTokenAge } from './tokenAge.ts';
import type { PoolInfo } from './pool.ts';
import type { Candidate, EnrichmentData, TokenMetadata } from './types.ts';

/**
 * Candidate enrichment (Section 5). Fetches pool/mint/holder/metadata data in
 * parallel under a global budget; anything that misses the budget or errors is
 * recorded as an "unknown" and handled per the unknowns policy (Section 6.3) by
 * the guardrail engine.
 */

const SOCIAL_KEYS = ['twitter', 'telegram', 'website', 'discord'];

export interface EnricherDeps {
  rpc: RpcClient;
  budgetMs: number;
  /** "not a Token mint" retry schedule for holders (config.guardrails.holdersNotMintRetryDelaysMs). */
  holdersRetryDelaysMs?: readonly number[];
  /**
   * Early-flow sampling window, ms (config.guardrails.momentumWindowMs). When
   * > 0, enrichment observes net SOL inflow for this long after graduation,
   * delaying screening by roughly this much. 0 disables it.
   */
  momentumWindowMs?: number;
  /** Optional per-candidate A/B buckets. Empty falls back to momentumWindowMs. */
  momentumWindowBucketsMs?: number[];
  /** Injectable RNG for deterministic bucket-selection tests. */
  rng?: () => number;
  /** When set, fetch the RugCheck advisory score; apiKey raises rate limits. */
  rugcheck?: { apiKey?: string };
  /** When true, fetch the pump.fun coin-age advisory signal. */
  tokenAge?: boolean;
}

export class Enricher {
  private readonly rpc: RpcClient;
  private readonly budgetMs: number;
  private readonly holdersRetryDelaysMs: readonly number[] | undefined;
  private readonly momentum: MomentumSampler;
  private readonly momentumWindowMs: number;
  private readonly momentumWindowBucketsMs: number[];
  private readonly rng: () => number;
  private readonly rugcheck: { apiKey?: string } | null;
  private readonly tokenAgeEnabled: boolean;
  private readonly log = logger.child({ mod: 'enrichment' });

  constructor(deps: EnricherDeps) {
    this.rpc = deps.rpc;
    this.budgetMs = deps.budgetMs;
    this.holdersRetryDelaysMs = deps.holdersRetryDelaysMs;
    this.momentum = new MomentumSampler({ rpc: deps.rpc });
    this.momentumWindowMs = deps.momentumWindowMs ?? 0;
    this.momentumWindowBucketsMs = deps.momentumWindowBucketsMs ?? [];
    this.rng = deps.rng ?? Math.random;
    this.rugcheck = deps.rugcheck ?? null;
    this.tokenAgeEnabled = deps.tokenAge ?? false;
  }

  async enrich(graduation: GraduationEvent): Promise<Candidate> {
    const started = Date.now();
    const deadline = started + this.budgetMs;
    const unknowns: string[] = [];

    const guard = async <T>(key: string, work: () => Promise<T>): Promise<T | undefined> => {
      try {
        return await withDeadline(work(), deadline, key);
      } catch (err) {
        unknowns.push(key);
        this.log.debug('enrichment field unavailable', { mint: graduation.mint, key, err });
        return undefined;
      }
    };

    // One DAS getAsset serves metadata, authority backstop, creator backstop,
    // and the holder-supply hint — a single in-flight promise shared by every
    // consumer, so the DAS fields cost zero extra RPC and zero extra latency.
    const assetP = this.rpc.getAsset(graduation.mint);
    // Silent share for the holder-supply hint: a DAS miss here must not add a
    // new unknown key — the metadata/dasFields guards already report it.
    const assetForHolders: Promise<DasAsset | null> = assetP.catch(() => null);
    // Opportunistic only: fetchHolders starts getTokenLargestAccounts immediately
    // and races this hint against setImmediate so a slow getAsset cannot starve holders.
    const holdersSupplyHint = assetForHolders.then((a) => supplyHint(a));

    // Mint-account supply hint: the mint read below already decodes supply
    // (u64 at offset 36), while getTokenSupply can lag it by 3.5 s+ on a fresh
    // mint ("could not find account"). Share the in-flight promise — not a
    // second read — so parallelism is unchanged; fetchHolders prefers the
    // first defined hint (mint, then DAS) and still falls back to
    // getTokenSupply when neither is in hand.
    const mintInfoP = guard('mintInfo', async () => {
      const acct = await this.rpc.getAccountInfoBase64(graduation.mint);
      if (!acct) throw new Error('mint account not found');
      return decodeMint(acct.data, acct.owner);
    });
    const mintSupplyHint = mintInfoP.then((m) =>
      m ? { supply: m.supply, decimals: m.decimals } : undefined,
    );

    const [mintInfo, pool, holders, metadata, dasFields, rugcheck, tokenAge] = await Promise.all([
      mintInfoP,
      guard('pool', async () => {
        const p = await fetchPumpSwapPool(this.rpc, graduation.mint);
        if (!p) throw new Error('pool not found');
        return p;
      }),
      guard('holders', async () =>
        fetchHolders(this.rpc, graduation.mint, [mintSupplyHint, holdersSupplyHint], {
          ...(this.holdersRetryDelaysMs ? { largestRetryDelaysMs: this.holdersRetryDelaysMs } : {}),
          deadlineMs: deadline,
        }),
      ),
      guard('metadata', async () => this.parseMetadata(await assetP)),
      // Authority/creator backstop from the same getAsset. Must not hang the
      // whole enrich if DAS never returns — metadata already records that miss.
      withDeadline(
        assetP.then((a) => parseDasFields(a), () => ({}) as DasFields),
        deadline,
        'dasFields',
      ).catch(() => ({}) as DasFields),
      this.rugcheck
        ? guard('rugcheck', async () => {
            const key = this.rugcheck!.apiKey;
            const r = await fetchRugcheck(graduation.mint, key ? { apiKey: key } : {});
            if (!r) throw new Error('rugcheck unavailable');
            return r;
          })
        : Promise.resolve(undefined),
      this.tokenAgeEnabled
        ? guard('tokenAge', async () => {
            const r = await fetchTokenAge(graduation.mint);
            if (!r) throw new Error('token age unavailable');
            return r;
          })
        : Promise.resolve(undefined),
    ]);

    const enrichment: EnrichmentData = {
      unknowns,
      elapsedMs: Date.now() - started,
    };
    if (mintInfo) enrichment.mintInfo = mintInfo;
    if (pool) enrichment.pool = pool;
    if (holders) enrichment.holders = holders;
    if (metadata) enrichment.metadata = metadata;
    if (dasFields?.authorities) enrichment.dasAuthorities = dasFields.authorities;
    if (dasFields?.creators) enrichment.dasCreators = dasFields.creators;
    if (rugcheck) enrichment.rugcheckScore = rugcheck.score;
    if (tokenAge) enrichment.tokenAgeMs = Math.max(0, Date.now() - tokenAge.createdAtMs);

    this.log.debug('enrichment complete', {
      mint: graduation.mint,
      elapsedMs: enrichment.elapsedMs,
      unknowns,
    });

    // Early-flow momentum (`sampleMomentum`) is deliberately NOT run here: it
    // waits out its own sampling window (up to 1s, longer than a typical
    // enrichment pass), and the caller (GuardrailPipeline.screen) runs it
    // concurrently with the H4 sellability probe instead of stacking the two
    // serially — same data, same wait, just overlapped.
    return { graduation, enrichment };
  }

  /**
   * Early-flow momentum sampling — split out from `enrich()` so a caller can
   * run it concurrently with other post-enrichment work (the H4 sellability
   * probe) instead of paying both waits serially. Picks this candidate's
   * window bucket even when `pool` is absent, so `momentumWindowMs` is always
   * recorded for the stat, matching `enrich()`'s prior behavior.
   */
  async sampleMomentum(pool: PoolInfo | undefined): Promise<{
    momentumWindowMs: number;
    earlyFlow?: EarlyFlow;
    /** True only when sampling was attempted (pool present, window > 0) and came back empty/errored. */
    missed: boolean;
  }> {
    const momentumWindowMs = this.pickMomentumWindowMs();
    if (!pool || momentumWindowMs <= 0) return { momentumWindowMs, missed: false };
    try {
      const flow = await this.momentum.sample(pool.quoteVault, pool.quoteReserveLamports, momentumWindowMs);
      return flow ? { momentumWindowMs, earlyFlow: flow, missed: false } : { momentumWindowMs, missed: true };
    } catch (err) {
      this.log.debug('enrichment field unavailable', { key: 'earlyFlow', err });
      return { momentumWindowMs, missed: true };
    }
  }

  private pickMomentumWindowMs(): number {
    const buckets = this.momentumWindowBucketsMs;
    if (buckets.length === 0) return this.momentumWindowMs;
    const idx = Math.min(buckets.length - 1, Math.floor(this.rng() * buckets.length));
    return buckets[idx]!;
  }

  private parseMetadata(asset: DasAsset | null): TokenMetadata {
    if (!asset) throw new Error('no DAS asset');
    const md = asset.content?.metadata ?? {};
    const links = asset.content?.links ?? {};
    const hasSocials = SOCIAL_KEYS.some((k) => typeof links[k] === 'string' && links[k]!.length > 0);
    const meta: TokenMetadata = { hasSocials, links };
    if (md.name) meta.name = md.name;
    if (md.symbol) meta.symbol = md.symbol;
    return meta;
  }
}

/**
 * Holder-supply hint from the shared DAS asset. Lets fetchHolders skip its
 * getTokenSupply call (one RPC saved per candidate). Conservative: used only
 * when supply is a safe non-negative integer and decimals look sane;
 * otherwise null and the direct read runs as before.
 */
export function supplyHint(asset: DasAsset | null | undefined): SupplyHint | undefined {
  const supply = asset?.token_info?.supply;
  const decimals = asset?.token_info?.decimals;
  if (
    typeof supply !== 'number' ||
    !Number.isInteger(supply) ||
    supply < 0 ||
    supply > Number.MAX_SAFE_INTEGER ||
    typeof decimals !== 'number' ||
    !Number.isInteger(decimals) ||
    decimals < 0 ||
    decimals > 18
  ) {
    return undefined;
  }
  return { supply: BigInt(supply), decimals };
}

export interface DasFields {
  authorities?: { mintAuthority?: string | null; freezeAuthority?: string | null };
  creators?: string[];
}

/**
 * Authority + creator backstop fields from the shared DAS asset. Returns
 * only fields DAS explicitly reports — absent stays absent (still unknown
 * downstream), null means explicitly revoked. Never throws: a DAS miss simply
 * yields no backstop fields (the metadata guard already reports the miss).
 */
export function parseDasFields(asset: DasAsset | null | undefined): DasFields {
  const out: DasFields = {};
  const mintAuthority = asset?.token_info?.mint_authority;
  const freezeAuthority = asset?.token_info?.freeze_authority;
  if (mintAuthority !== undefined || freezeAuthority !== undefined) {
    out.authorities = {};
    if (mintAuthority !== undefined) out.authorities.mintAuthority = mintAuthority;
    if (freezeAuthority !== undefined) out.authorities.freezeAuthority = freezeAuthority;
  }
  const creators = (asset?.creators ?? [])
    .filter((c) => typeof c.address === 'string' && c.address.length > 0)
    .sort((a, b) => Number(b.verified ?? false) - Number(a.verified ?? false))
    .map((c) => c.address);
  if (creators.length > 0) out.creators = [...new Set(creators)];
  return out;
}

/** Race a promise against a shared deadline. Rejects with a timeout past it. */
function withDeadline<T>(p: Promise<T>, deadlineMs: number, key: string): Promise<T> {
  const remaining = Math.max(0, deadlineMs - Date.now());
  return new Promise<T>((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error(`${key} exceeded enrichment budget`)), remaining);
    p.then(
      (v) => {
        clearTimeout(timer);
        resolve(v);
      },
      (e) => {
        clearTimeout(timer);
        reject(e);
      },
    );
  });
}
