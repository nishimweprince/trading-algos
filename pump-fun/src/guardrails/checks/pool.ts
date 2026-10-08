import type { CheckResult } from '../../core/types.ts';
import type { CheckContext } from '../engine.ts';
import { effectiveQuoteSol, quoteReserveSol } from '../../enrichment/pool.ts';
import { entrySizeLadder } from '../../config/sizing.ts';

/**
 * Pool-backed hard check (H7). Reads only the fast read's snapshot. (H6,
 * creator holdings, was removed 2026-10-04.)
 *
 * H3 (LP burned) and H5 (holder concentration) are gone: a canonical
 * migration burns the LP (P0), and H5 needed getTokenLargestAccounts, which
 * does not index a fresh mint in time — 99 of 174 candidates on 2026-09-28
 * came back "holders unavailable".
 */

/**
 * H7 — liquidity floor + buy price impact. For a constant-product pool the price
 * impact of buying `dy` SOL is exactly dy / quoteReserve, so the intended buy
 * must stay under the configured cap and the pool must clear the SOL floor.
 * The floor is real liquidity (vault SOL); impact uses the effective reserve
 * (vault + virtual) the program prices against.
 */
export function checkLiquidityFloor(ctx: CheckContext): CheckResult {
  const pool = ctx.candidate.enrichment.pool;
  if (!pool) return { id: 'H7', label: 'Liquidity floor + buy impact', status: 'fail', detail: 'pool unavailable' };

  const reserveSol = quoteReserveSol(pool);
  const minSol = ctx.config.guardrails.minPoolSol;
  const sizeSol = entrySizeLadder(ctx.config, ctx.walletSol).baseSol;
  const pricedSol = effectiveQuoteSol(pool);
  const impactPct = pricedSol > 0 ? (sizeSol / pricedSol) * 100 : 100;
  const maxImpact = ctx.config.guardrails.maxBuyImpactPct;

  if (reserveSol < minSol) {
    return fail('H7', 'Liquidity floor + buy impact', `reserve ${reserveSol.toFixed(1)} SOL < ${minSol}`);
  }
  if (impactPct > maxImpact) {
    return fail('H7', 'Liquidity floor + buy impact', `buy impact ${impactPct.toFixed(2)}% > ${maxImpact}%`);
  }
  return ok('H7', 'Liquidity floor + buy impact', `reserve ${reserveSol.toFixed(1)} SOL, impact ${impactPct.toFixed(2)}%`);
}

function ok(id: string, label: string, detail: string): CheckResult {
  return { id, label, status: 'pass', detail };
}
function fail(id: string, label: string, detail: string): CheckResult {
  return { id, label, status: 'fail', detail };
}

