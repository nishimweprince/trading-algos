import type { CheckResult } from '../../core/types.ts';
import type { CheckContext } from '../engine.ts';

/**
 * H13 — manipulation screens (work plan 2026-09-25 P3.3). Enforces the
 * thresholds configured under guardrails.features; every feature is
 * advisory (pass) when absent or unconfigured. The cluster clause finally
 * enforces `creatorMaxLaunches7d`, at the level of the funding cluster.
 */
export function checkManipulation(ctx: CheckContext): CheckResult {
  const id = 'H13';
  const label = 'Manipulation screens';
  const f = ctx.candidate.enrichment.features;
  const cfg = ctx.config.guardrails.features;
  if (!cfg.enabled || !f) return { id, label, status: 'pass', detail: 'features off / not computed' };

  const cap = ctx.config.guardrails.creatorMaxLaunches7d;
  if (cfg.cluster.veto && f.cluster && cap > 0 && f.cluster.launches7d > cap) {
    return {
      id, label, status: 'fail', reason: 'creator_cluster',
      detail: `funding cluster ${f.cluster.root.slice(0, 6)}… launched ${f.cluster.launches7d} coins in 7 d (> ${cap}, ${f.cluster.wallets} wallets)`,
    };
  }
  if (cfg.maxBundleSharePct !== undefined && f.curve?.bundleSharePct != null && f.curve.bundleSharePct > cfg.maxBundleSharePct) {
    return { id, label, status: 'fail', reason: 'bundled_launch', detail: `${f.curve.bundleSharePct.toFixed(1)}% of supply bought in the creation slot` };
  }
  if (cfg.maxWashRatio !== undefined && f.curve?.washRatio != null && f.curve.washRatio > cfg.maxWashRatio) {
    return { id, label, status: 'fail', reason: 'wash_trading', detail: `wash ratio ${(f.curve.washRatio * 100).toFixed(0)}%` };
  }
  if (cfg.maxSniperBuyShare !== undefined && f.snipers && f.snipers.sniperBuyShare > cfg.maxSniperBuyShare) {
    return { id, label, status: 'fail', reason: 'sniper_dominated', detail: `known snipers bought ${(f.snipers.sniperBuyShare * 100).toFixed(0)}% of early volume` };
  }
  if (cfg.vetoCopycat && f.copycat?.isCopycat) {
    return { id, label, status: 'fail', reason: 'copycat', detail: `name matches ${f.copycat.nameMatches}, image matches ${f.copycat.imageMatches}` };
  }
  return { id, label, status: 'pass', detail: 'no manipulation threshold breached' };
}
