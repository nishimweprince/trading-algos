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
 * H7 — liquidity band + buy price impact. For a constant-product pool the price
 * impact of buying `dy` SOL is exactly dy / quoteReserve, so the intended buy
 * must stay under the configured cap and the pool must sit inside the SOL band.
 * The band is real liquidity (vault SOL); impact uses the effective reserve
 * (vault + virtual) the program prices against.
 *
 * The ceiling catches pools pumped inside the migrate slot. A canonical
 * migration opens at ~68 SOL real (~85 effective incl. virtual); price scales
 * with the square of the effective reserve, so a 230 SOL real pool at our
 * read is ~8x the open price. Live 2026-10-06: three such
 * entries (224 / 233 / 1275 SOL, creator-called migrates with a bundled buy)
 * all ended in EMERGENCY_EXIT; the 2026-10-04/05 dry run had 113 entries over
 * 200 SOL at a 19 % win rate, 101 of them emergency exits.
 */
export function checkLiquidityFloor(ctx: CheckContext): CheckResult {
  const pool = ctx.candidate.enrichment.pool;
  if (!pool) return { id: 'H7', label: 'Liquidity floor + buy impact', status: 'fail', detail: 'pool unavailable' };

  const reserveSol = quoteReserveSol(pool);
  const minSol = ctx.config.guardrails.minPoolSol;
  const maxSol = ctx.config.guardrails.maxPoolSol;
  const sizeSol = entrySizeLadder(ctx.config, ctx.walletSol).baseSol;
  const pricedSol = effectiveQuoteSol(pool);
  const impactPct = pricedSol > 0 ? (sizeSol / pricedSol) * 100 : 100;
  const maxImpact = ctx.config.guardrails.maxBuyImpactPct;

  if (reserveSol < minSol) {
    return fail('H7', 'Liquidity floor + buy impact', `reserve ${reserveSol.toFixed(1)} SOL < ${minSol}`);
  }
  if (reserveSol > maxSol) {
    return fail('H7', 'Liquidity floor + buy impact', `reserve ${reserveSol.toFixed(1)} SOL > ${maxSol} (pumped in the migrate slot)`);
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

