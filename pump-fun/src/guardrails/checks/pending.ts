import type { CheckResult } from '../../core/types.ts';
import type { CheckContext } from '../engine.ts';

/**
 * H4 — sellability (honeypot / transfer-tax trap). The atomic buy+sell probe
 * (executor/sellability.ts) runs during screening in dry-run/live with a funded
 * wallet and attaches its result to enrichment.sellable; this check reads it.
 *
 * When the probe can't run (paper mode / no funded wallet) it reports `unknown`
 * — which vetoes in live per the unknowns policy. H1/H2 (authorities) and H9
 * (Token-2022 extensions) already catch the static honeypot vectors (freeze,
 * transfer fee, transfer hook, non-transferable) on-chain; H4 is the dynamic
 * backstop on top.
 */
export function checkSellability(ctx: CheckContext): CheckResult {
  const s = ctx.candidate.enrichment.sellable;
  if (!s) {
    return {
      id: 'H4',
      label: 'Sellable (no honeypot)',
      status: 'unknown',
      reason: 'not_run',
      detail: 'sell simulation not run',
    };
  }
  // Explicit early-move gate (guardrails.maxProbeMovePct). Reported as
  // `price_moved` on purpose: the engine never tolerates that reason, so the
  // 2026-09-16 "entered behind a spike" protection applies unchanged — but now
  // at a threshold the operator sets, not as a side effect of the probe bound.
  const cap = ctx.config.guardrails.maxProbeMovePct;
  if (cap !== undefined && s.poolMovePct !== undefined && s.poolMovePct > cap) {
    return {
      id: 'H4',
      label: 'Sellable (no honeypot)',
      status: 'unknown',
      reason: 'price_moved',
      detail: `pool moved +${s.poolMovePct.toFixed(1)}% since enrichment > maxProbeMovePct ${cap}% (${s.detail})`,
    };
  }
  return {
    id: 'H4',
    label: 'Sellable (no honeypot)',
    status: s.status,
    ...(s.reason ? { reason: s.reason } : {}),
    detail: s.detail,
  };
}
