import type { RpcClient } from '../../core/rpc.ts';
import type { Repositories } from '../../persistence/repositories.ts';
import { logger } from '../../core/logger.ts';
import { creatorCluster } from './cluster.ts';

type Rpc = Pick<RpcClient, 'getSignaturesForAddress' | 'getParsedTransaction'>;

export interface ClusterWarmerOptions {
  hops: 1 | 2;
  maxSigs: number;
  maxConcurrent: number;
  maxQueue: number;
}

/**
 * Pre-graduation H13 precompute. Each new launch's creator is resolved to its
 * funding root in the background (creatorCluster writes wallet_funders), so
 * the verdict only reads the cache. A coin spends minutes on its curve before
 * it can graduate, which is ample time at ~0.3 launches/s.
 *
 * Newest first (LIFO): under a backlog the freshest launches — the only ones
 * that can still graduate soon — are resolved first, and the oldest queued
 * are dropped past `maxQueue`. Creators already in wallet_funders are free.
 */
export class ClusterWarmer {
  private readonly rpc: Rpc;
  private readonly repos: Pick<Repositories, 'walletFunder' | 'upsertWalletFunder' | 'clusterLaunchCount'>;
  private readonly opts: ClusterWarmerOptions;
  private readonly log = logger.child({ mod: 'cluster-warm' });
  private readonly queue: string[] = [];
  private readonly queued = new Set<string>();
  private active = 0;
  private stopped = false;
  readonly stats = { enqueued: 0, cached: 0, dropped: 0, resolved: 0, failed: 0 };

  constructor(
    rpc: Rpc,
    repos: Pick<Repositories, 'walletFunder' | 'upsertWalletFunder' | 'clusterLaunchCount'>,
    opts: ClusterWarmerOptions,
  ) {
    this.rpc = rpc;
    this.repos = repos;
    this.opts = opts;
  }

  enqueue(creator: string): void {
    if (this.stopped || this.queued.has(creator)) return;
    if (this.repos.walletFunder(creator)) {
      this.stats.cached++;
      return;
    }
    this.queue.push(creator);
    this.queued.add(creator);
    this.stats.enqueued++;
    while (this.queue.length > this.opts.maxQueue) {
      const dropped = this.queue.shift()!;
      this.queued.delete(dropped);
      this.stats.dropped++;
    }
    this.pump();
  }

  stop(): void {
    this.stopped = true;
    this.queue.length = 0;
    this.queued.clear();
  }

  get pending(): number {
    return this.queue.length + this.active;
  }

  private pump(): void {
    while (!this.stopped && this.active < this.opts.maxConcurrent && this.queue.length > 0) {
      const creator = this.queue.pop()!;
      this.queued.delete(creator);
      this.active++;
      creatorCluster(this.rpc, this.repos, creator, { hops: this.opts.hops, maxSigs: this.opts.maxSigs })
        .then(() => {
          this.stats.resolved++;
        })
        .catch((err) => {
          this.stats.failed++;
          this.log.debug('cluster warm failed', { creator, err });
        })
        .finally(() => {
          this.active--;
          this.pump();
        });
    }
  }
}

