import type { CheckResult } from '../../core/types.ts';
import type { CheckContext } from '../engine.ts';
import { quoteReserveSol } from '../../enrichment/pool.ts';
import { entrySizeLadder } from '../../config/sizing.ts';

/**
 * Pool-backed hard checks (H6, H7). Both read only the fast read's snapshot.
 *
 * H3 (LP burned) and H5 (holder concentration) are gone: a canonical
 * migration burns the LP (P0), and H5 needed getTokenLargestAccounts, which
 * does not index a fresh mint in time — 99 of 174 candidates on 2026-09-28
 * came back "holders unavailable".
 */

/**
 * H6 — creator (dev) holdings under cap, from the creator's own ATA in the
 * fast read. The creator is only known before the read when the launch feed
 * saw the creation (it names the fee payer); if that is not the pool's
 * coin_creator, or the launch was not seen, the check does not veto.
 */
export function checkCreatorHoldings(ctx: CheckContext): CheckResult {
  const id = 'H6';
  const label = 'Creator holdings under cap';
  const held = ctx.candidate.enrichment.creatorHolding;
  if (!held) return { id, label, status: 'pass', reason: 'not_checked', detail: 'creator not known before graduation — not checked' };
  const cap = ctx.config.guardrails.creatorHoldingsCapPct / 100;
  if (held.share > cap) {
    return { id, label, status: 'fail', detail: `creator holds ${pct(held.share)} > ${pct(cap)}` };
  }
  return { id, label, status: 'pass', detail: `creator holds ${pct(held.share)}` };
}

/**
 * H7 — liquidity floor + buy price impact. For a constant-product pool the price
 * impact of buying `dy` SOL is exactly dy / quoteReserve, so the intended buy
 * must stay under the configured cap and the pool must clear the SOL floor.
 */
export function checkLiquidityFloor(ctx: CheckContext): CheckResult {
  const pool = ctx.candidate.enrichment.pool;
  if (!pool) return { id: 'H7', label: 'Liquidity floor + buy impact', status: 'fail', detail: 'pool unavailable' };

  const reserveSol = quoteReserveSol(pool);
  const minSol = ctx.config.guardrails.minPoolSol;
  const sizeSol = entrySizeLadder(ctx.config, ctx.walletSol).baseSol;
  const impactPct = reserveSol > 0 ? (sizeSol / reserveSol) * 100 : 100;
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
function pct(x: number): string {
  return `${(x * 100).toFixed(1)}%`;
}
