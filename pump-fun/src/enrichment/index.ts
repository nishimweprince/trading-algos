import type { RpcClient, DasAsset } from '../core/rpc.ts';
import type { GraduationEvent } from '../core/types.ts';
import { logger } from '../core/logger.ts';
import { decodeMint } from './mint.ts';
import { fetchHolders, type SupplyHint } from './holders.ts';
import { fetchPumpSwapPool } from './pool.ts';
import { MomentumSampler, type EarlyFlow } from './momentum.ts';
import { fetchRugcheck } from './rugcheck.ts';
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
}

export class Enricher {
  private readonly rpc: RpcClient;
  private readonly budgetMs: number;
  private readonly momentum: MomentumSampler;
  private readonly momentumWindowMs: number;
  private readonly momentumWindowBucketsMs: number[];
  private readonly rng: () => number;
  private readonly rugcheck: { apiKey?: string } | null;
  private readonly log = logger.child({ mod: 'enrichment' });

  constructor(deps: EnricherDeps) {
    this.rpc = deps.rpc;
    this.budgetMs = deps.budgetMs;
    this.momentum = new MomentumSampler({ rpc: deps.rpc });
    this.momentumWindowMs = deps.momentumWindowMs ?? 0;
    this.momentumWindowBucketsMs = deps.momentumWindowBucketsMs ?? [];
    this.rng = deps.rng ?? Math.random;
    this.rugcheck = deps.rugcheck ?? null;
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

    const [mintInfo, pool, holders, metadata, dasFields, rugcheck] = await Promise.all([
      guard('mintInfo', async () => {
        const acct = await this.rpc.getAccountInfoBase64(graduation.mint);
        if (!acct) throw new Error('mint account not found');
        return decodeMint(acct.data, acct.owner);
      }),
      guard('pool', async () => {
        const p = await fetchPumpSwapPool(this.rpc, graduation.mint);
        if (!p) throw new Error('pool not found');
        return p;
      }),
      guard('holders', async () => fetchHolders(this.rpc, graduation.mint, supplyHint(await assetForHolders))),
      guard('metadata', async () => this.parseMetadata(await assetP)),
      // Bare (unguarded) share: never rejects, never adds an unknown key.
      assetP.then((a) => parseDasFields(a), () => ({}) as DasFields),
      this.rugcheck
        ? guard('rugcheck', async () => {
            const key = this.rugcheck!.apiKey;
            const r = await fetchRugcheck(graduation.mint, key ? { apiKey: key } : {});
            if (!r) throw new Error('rugcheck unavailable');
            return r;
          })
        : Promise.resolve(undefined),
    ]);

    // Early-flow momentum runs AFTER the budgeted enrichment: it deliberately
    // waits out the sampling window (longer than the enrichment budget) rather
    // than racing the shared deadline, so it is guarded separately. Best-effort —
    // a miss is recorded as unknown and simply omits the soft signal.
    let earlyFlow: EarlyFlow | undefined;
    const momentumWindowMs = this.pickMomentumWindowMs();
    if (pool && momentumWindowMs > 0) {
      try {
        const flow = await this.momentum.sample(pool.quoteVault, pool.quoteReserveLamports, momentumWindowMs);
        if (flow) earlyFlow = flow;
        else unknowns.push('earlyFlow');
      } catch (err) {
        unknowns.push('earlyFlow');
        this.log.debug('enrichment field unavailable', { mint: graduation.mint, key: 'earlyFlow', err });
      }
    }

    const enrichment: EnrichmentData = {
      unknowns,
      elapsedMs: Date.now() - started,
      momentumWindowMs,
    };
    if (mintInfo) enrichment.mintInfo = mintInfo;
    if (pool) enrichment.pool = pool;
    if (holders) enrichment.holders = holders;
    if (metadata) enrichment.metadata = metadata;
    if (dasFields?.authorities) enrichment.dasAuthorities = dasFields.authorities;
    if (dasFields?.creators) enrichment.dasCreators = dasFields.creators;
    if (earlyFlow) enrichment.earlyFlow = earlyFlow;
    if (rugcheck) enrichment.rugcheckScore = rugcheck.score;

    this.log.debug('enrichment complete', {
      mint: graduation.mint,
      elapsedMs: enrichment.elapsedMs,
      unknowns,
    });

    return { graduation, enrichment };
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
