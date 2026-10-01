import type { CheckResult } from '../../core/types.ts';
import type { CheckContext } from '../engine.ts';

/**
 * H13 — serial-launcher funding cluster, from the PRECOMPUTED cache only.
 *
 * Walking a creator's funding graph costs several RPC round trips, so it no
 * longer runs at the verdict. The cluster warmer (enrichment/features/
 * clusterWarm.ts) resolves each creator's funding root when the coin
 * launches — minutes before it can graduate — and the background enrichment
 * resolves any it missed, for that creator's next coin. Here it is two
 * indexed SQLite reads: the creator's cached root, then the cluster's 7-day
 * launch count. Not cached → not checked, never a veto.
 *
 * The other manipulation features (bundle share, wash ratio, snipers,
 * copycat) stay research-only: they need the curve's history, which is not
 * available in time.
 */
export function checkManipulation(ctx: CheckContext): CheckResult {
  const id = 'H13';
  const label = 'Creator funding cluster';
  const cfg = ctx.config.guardrails.features;
  const cap = ctx.config.guardrails.creatorMaxLaunches7d;
  const creator = ctx.candidate.enrichment.pool?.coinCreator;
  if (!cfg.enabled || !cfg.cluster.enabled || !cfg.cluster.veto || cap <= 0) {
    return { id, label, status: 'pass', detail: 'cluster veto off' };
  }
  if (!creator) return { id, label, status: 'pass', reason: 'not_checked', detail: 'no creator' };
  const cached = ctx.repos.walletFunder(creator);
  if (!cached) return { id, label, status: 'pass', reason: 'not_checked', detail: 'cluster not precomputed' };
  const root = cached.root ?? creator;
  const counts = ctx.repos.clusterLaunchCount(root, 7);
  if (counts.launches > cap) {
    return {
      id,
      label,
      status: 'fail',
      reason: 'creator_cluster',
      detail: `funding cluster ${root.slice(0, 6)}… launched ${counts.launches} coins in 7 d (> ${cap}, ${counts.wallets} wallets)`,
    };
  }
  return { id, label, status: 'pass', detail: `cluster ${root.slice(0, 6)}… launched ${counts.launches} in 7 d` };
}
