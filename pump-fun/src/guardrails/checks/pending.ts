import type { CheckResult } from '../../core/types.ts';
import type { CheckContext } from '../engine.ts';

/**
 * Pool- and swap-dependent hard checks (H3, H4, H5, H6, H7).
 *
 * These require artifacts that are safety-critical to get exactly right and that
 * we will NOT guess: a verified PumpSwap pool-account decoder (Phase 2b) and a
 * sell-simulation swap builder (Phase 4). Rather than ship a plausible-but-
 * unverified layout in the module whose whole job is preventing losses, each
 * returns `unknown` with a precise reason.
 *
 * Consequence (Section 6.3, and correct): in LIVE mode `unknown` == FAIL, so
 * these veto every entry until implemented — the safe default. In paper mode
 * they don't veto; the raw enrichment data still accumulates for tuning.
 */

export function checkLpStatus(_ctx: CheckContext): CheckResult {
  return {
    id: 'H3',
    label: 'LP burned or locked',
    status: 'unknown',
    detail: 'pending verified PumpSwap pool decoder (Phase 2b)',
  };
}

export function checkSellability(_ctx: CheckContext): CheckResult {
  return {
    id: 'H4',
    label: 'Sellable (no honeypot)',
    status: 'unknown',
    detail: 'pending sell-simulation swap builder (Phase 4)',
  };
}

/**
 * H5 — holder concentration. Enrichment already fetches the largest holders, but
 * an accurate cap check must exclude the pool vault and burn addresses, which
 * needs the pool's known vault addresses (Phase 2b). Raw figures are surfaced in
 * `detail` so paper-mode data captures them.
 */
export function checkHolderConcentration(ctx: CheckContext): CheckResult {
  const h = ctx.candidate.enrichment.holders;
  if (!h) {
    return { id: 'H5', label: 'Holder concentration', status: 'unknown', detail: 'holders unavailable' };
  }
  return {
    id: 'H5',
    label: 'Holder concentration',
    status: 'unknown',
    detail: `raw top10=${pct(h.top10Share)} max=${pct(h.maxShare)} (pool-vault exclusion pending Phase 2b)`,
  };
}

export function checkCreatorHoldings(_ctx: CheckContext): CheckResult {
  return {
    id: 'H6',
    label: 'Creator holdings under cap',
    status: 'unknown',
    detail: 'pending creator identification (Phase 2b)',
  };
}

export function checkLiquidityFloor(_ctx: CheckContext): CheckResult {
  return {
    id: 'H7',
    label: 'Liquidity floor + buy impact',
    status: 'unknown',
    detail: 'pending PumpSwap pool reserves decode (Phase 2b)',
  };
}

function pct(x: number): string {
  return `${(x * 100).toFixed(1)}%`;
}
