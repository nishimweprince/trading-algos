import type { RpcClient, DasAsset } from '../core/rpc.ts';
import type { GraduationEvent } from '../core/types.ts';
import { logger } from '../core/logger.ts';
import { decodeMint } from './mint.ts';
import { fetchHolders } from './holders.ts';
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
}

export class Enricher {
  private readonly rpc: RpcClient;
  private readonly budgetMs: number;
  private readonly log = logger.child({ mod: 'enrichment' });

  constructor(deps: EnricherDeps) {
    this.rpc = deps.rpc;
    this.budgetMs = deps.budgetMs;
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

    const [mintInfo, holders, metadata] = await Promise.all([
      guard('mintInfo', async () => {
        const acct = await this.rpc.getAccountInfoBase64(graduation.mint);
        if (!acct) throw new Error('mint account not found');
        return decodeMint(acct.data, acct.owner);
      }),
      guard('holders', () => fetchHolders(this.rpc, graduation.mint)),
      guard('metadata', async () => this.parseMetadata(await this.rpc.getAsset(graduation.mint))),
    ]);

    const enrichment: EnrichmentData = {
      unknowns,
      elapsedMs: Date.now() - started,
    };
    if (mintInfo) enrichment.mintInfo = mintInfo;
    if (holders) enrichment.holders = holders;
    if (metadata) enrichment.metadata = metadata;

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
