import type { RpcClient } from '../../core/rpc.ts';
import type { Repositories } from '../../persistence/repositories.ts';
import type { Config } from '../../config/schema.ts';
import type { Candidate } from '../types.ts';
import type { SwapEvent } from '../txFlow.ts';
import { logger } from '../../core/logger.ts';
import { deriveBondingCurvePda } from '../curve.ts';
import { creatorCluster } from './cluster.ts';
import { curveFeatures } from './curve.ts';
import { copycatFeatures } from './copycat.ts';
import { sniperFeatures } from './snipers.ts';
import { holderQuality } from './holderQuality.ts';
import { mintAgeAtMigration, MS_PER_SLOT } from '../../guardrails/checks/population.ts';
import type { ManipulationFeatures } from './types.ts';

export type { ManipulationFeatures } from './types.ts';

type Rpc = Pick<RpcClient, 'getSignaturesForAddress' | 'getParsedTransaction'>;
type FeaturesConfig = Config['guardrails']['features'];

/**
 * Runs the enabled manipulation features for one candidate, each bounded by
 * the shared `budgetMs` deadline (work plan 2026-09-25 P3.3). A feature that
 * throws or misses the deadline lands in `missing`; the rest still return.
 */
export class FeatureEngine {
  private readonly rpc: Rpc;
  private readonly repos: Repositories;
  private readonly cfg: FeaturesConfig;
  private readonly now: () => number;
  private readonly log = logger.child({ mod: 'features' });

  constructor(deps: { rpc: Rpc; repos: Repositories; config: FeaturesConfig; now?: () => number }) {
    this.rpc = deps.rpc;
    this.repos = deps.repos;
    this.cfg = deps.config;
    this.now = deps.now ?? Date.now;
  }

  get enabled(): boolean {
    return this.cfg.enabled;
  }

  /** RPC-bound features; run concurrently with the H4 probe / momentum wait. */
  async compute(c: Candidate): Promise<ManipulationFeatures> {
    const out: ManipulationFeatures = {};
    if (!this.cfg.enabled) return out;
    const missing: string[] = [];
    const deadlineMs = this.now() + this.cfg.budgetMs;
    const e = c.enrichment;
    const creator = e.pool?.coinCreator ?? e.dasCreators?.[0];

    const tasks: Array<Promise<void>> = [];
    const run = (key: string, work: () => Promise<void>) =>
      tasks.push(
        withDeadline(work(), deadlineMs - this.now()).catch((err) => {
          missing.push(key);
          this.log.debug('feature unavailable', { mint: c.graduation.mint, key, err });
        }),
      );

    if (this.cfg.cluster.enabled && creator) {
      run('cluster', async () => {
        out.cluster = await creatorCluster(this.rpc, this.repos, creator, {
          hops: this.cfg.cluster.hops,
          maxSigs: this.cfg.cluster.maxSigs,
        });
      });
    }
    const curvePda = deriveBondingCurvePda(c.graduation.mint);
    const supply = e.mintInfo?.supply ?? e.holders?.supply;
    if (this.cfg.curve.enabled && curvePda && supply !== undefined) {
      run('curve', async () => {
        out.curve = await curveFeatures(this.rpc, curvePda, c.graduation.mint, supply, {
          maxPages: this.cfg.curve.maxPages,
          maxCreationTx: this.cfg.curve.maxCreationTx,
          washSampleTx: this.cfg.curve.washSampleTx,
          deadlineMs,
          now: this.now,
        });
      });
    }
    if (this.cfg.copycat.enabled) {
      try {
        out.copycat = copycatFeatures(this.repos, c.graduation.mint, e.metadata);
      } catch (err) {
        missing.push('copycat');
        this.log.debug('feature unavailable', { key: 'copycat', err });
      }
    }
    if (this.cfg.holderQuality.enabled && e.holders && e.pool) {
      const skip = new Set([e.pool.baseVault, e.pool.quoteVault, e.pool.poolAddress, curvePda ?? '']);
      const owners = e.holders.holders.map((h) => h.owner).filter((o): o is string => Boolean(o) && !skip.has(o!));
      run('holderQuality', async () => {
        out.holderQuality = await holderQuality(this.rpc, owners, this.cfg.holderQuality);
      });
    }

    await Promise.all(tasks);

    // Time to graduate: curve creation slot is the on-chain truth; else the
    // population check's mint-age sources.
    if (out.curve?.creationSlot && c.graduation.slot > 0) {
      out.timeToGraduateMs = Math.max(0, (c.graduation.slot - out.curve.creationSlot) * MS_PER_SLOT);
    } else {
      out.timeToGraduateMs = mintAgeAtMigration(c, this.repos)?.ms ?? null;
    }
    if (missing.length) out.missing = missing;
    return out;
  }

  /** DB-only features that need the momentum sample's swaps. */
  addEarlyFlowFeatures(c: Candidate, f: ManipulationFeatures, swaps: readonly SwapEvent[] | undefined): void {
    if (!this.cfg.enabled || !this.cfg.snipers.enabled || !swaps?.length) return;
    try {
      f.snipers = sniperFeatures(this.repos, c.graduation.mint, swaps, this.cfg.snipers);
    } catch (err) {
      (f.missing ??= []).push('snipers');
      this.log.debug('feature unavailable', { key: 'snipers', err });
    }
  }
}

function withDeadline<T>(p: Promise<T>, remainingMs: number): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error('feature budget exceeded')), Math.max(0, remainingMs));
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
