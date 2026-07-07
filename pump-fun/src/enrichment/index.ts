import type { RpcClient, DasAsset } from '../core/rpc.ts';
import type { GraduationEvent } from '../core/types.ts';
import { logger } from '../core/logger.ts';
import { decodeMint } from './mint.ts';
import { fetchHolders } from './holders.ts';
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
  /** When set, fetch the RugCheck advisory score; apiKey raises rate limits. */
  rugcheck?: { apiKey?: string };
}

export class Enricher {
  private readonly rpc: RpcClient;
  private readonly budgetMs: number;
  private readonly momentum: MomentumSampler | null;
  private readonly rugcheck: { apiKey?: string } | null;
  private readonly log = logger.child({ mod: 'enrichment' });

  constructor(deps: EnricherDeps) {
    this.rpc = deps.rpc;
    this.budgetMs = deps.budgetMs;
    this.momentum =
      deps.momentumWindowMs && deps.momentumWindowMs > 0
        ? new MomentumSampler({ rpc: deps.rpc, windowMs: deps.momentumWindowMs })
        : null;
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

    const [mintInfo, pool, holders, metadata, rugcheck] = await Promise.all([
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
      guard('holders', () => fetchHolders(this.rpc, graduation.mint)),
      guard('metadata', async () => this.parseMetadata(await this.rpc.getAsset(graduation.mint))),
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
    if (pool && this.momentum) {
      try {
        const flow = await this.momentum.sample(pool.quoteVault, pool.quoteReserveLamports);
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
    };
    if (mintInfo) enrichment.mintInfo = mintInfo;
    if (pool) enrichment.pool = pool;
    if (holders) enrichment.holders = holders;
    if (metadata) enrichment.metadata = metadata;
    if (earlyFlow) enrichment.earlyFlow = earlyFlow;
    if (rugcheck) enrichment.rugcheckScore = rugcheck.score;

    this.log.debug('enrichment complete', {
      mint: graduation.mint,
      elapsedMs: enrichment.elapsedMs,
      unknowns,
    });

    return { graduation, enrichment };
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
