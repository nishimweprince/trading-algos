import type { CheckResult } from '../../core/types.ts';
import type { CheckContext } from '../engine.ts';
import { quoteReserveSol, BURN_OWNERS } from '../../enrichment/pool.ts';

/**
 * Pool-backed hard checks (H3, H5, H6, H7), enabled by the verified PumpSwap
 * pool decoder. Each requires the decoded pool; without it they report unknown.
 */

/**
 * H3 — LP burned or locked. Canonical pump.fun migrations burn the pool's LP to
 * lock liquidity, so the SPL lp_mint circulating supply is 0 (verified live).
 * A non-zero LP supply on a graduation pool means liquidity is withdrawable —
 * the direct liquidity-rug vector.
 */
export function checkLpStatus(ctx: CheckContext): CheckResult {
  const pool = ctx.candidate.enrichment.pool;
  if (!pool) return unk('H3', 'LP burned or locked', 'pool unavailable');
  if (pool.lpMintSupply === 0n) {
    return ok('H3', 'LP burned or locked', 'lp_mint supply 0 (burned)');
  }
  return fail('H3', 'LP burned or locked', `lp_mint supply ${pool.lpMintSupply} not burned — withdrawable`);
}

/**
 * H5 — top-holder concentration, excluding the pool vaults and burn addresses.
 * The pool base vault is (correctly) the largest token holder; counting it would
 * flag every token, so it is removed before the cap check.
 */
export function checkHolderConcentration(ctx: CheckContext): CheckResult {
  const { holders, pool } = ctx.candidate.enrichment;
  if (!holders) return unk('H5', 'Holder concentration', 'holders unavailable');
  if (!pool) return unk('H5', 'Holder concentration', 'pool needed to exclude vault');

  const excludedAccounts = new Set([pool.baseVault, pool.quoteVault]);
  const real = holders.holders.filter(
    (h) => !excludedAccounts.has(h.account) && !(h.owner && BURN_OWNERS.has(h.owner)),
  );
  const top10 = real.slice(0, 10).reduce((s, h) => s + h.share, 0);
  const maxShare = real[0]?.share ?? 0;

  const top10Cap = ctx.config.guardrails.top10HolderCapPct / 100;
  const singleCap = ctx.config.guardrails.singleHolderCapPct / 100;

  if (top10 > top10Cap) {
    return fail('H5', 'Holder concentration', `top10 ${pct(top10)} > ${pct(top10Cap)}`);
  }
  if (maxShare > singleCap) {
    return fail('H5', 'Holder concentration', `largest holder ${pct(maxShare)} > ${pct(singleCap)}`);
  }
  return ok('H5', 'Holder concentration', `top10 ${pct(top10)}, max ${pct(maxShare)}`);
}

/**
 * H6 — creator (dev) holdings under cap. Sums the shares of holders owned by the
 * pool's coin_creator. Linked-wallet clustering (same funding source) is a
 * future enhancement; visible holdings are checked now.
 */
export function checkCreatorHoldings(ctx: CheckContext): CheckResult {
  const { holders, pool } = ctx.candidate.enrichment;
  if (!holders) return unk('H6', 'Creator holdings under cap', 'holders unavailable');
  if (!pool) return unk('H6', 'Creator holdings under cap', 'pool needed to identify creator');

  const creatorShare = holders.holders
    .filter((h) => h.owner === pool.coinCreator)
    .reduce((s, h) => s + h.share, 0);
  const cap = ctx.config.guardrails.creatorHoldingsCapPct / 100;

  if (creatorShare > cap) {
    return fail('H6', 'Creator holdings under cap', `creator holds ${pct(creatorShare)} > ${pct(cap)}`);
  }
  return ok('H6', 'Creator holdings under cap', `creator holds ${pct(creatorShare)}`);
}

/**
 * H7 — liquidity floor + buy price impact. For a constant-product pool the price
 * impact of buying `dy` SOL is exactly dy / quoteReserve, so the intended buy
 * must stay under the configured cap and the pool must clear the SOL floor.
 */
export function checkLiquidityFloor(ctx: CheckContext): CheckResult {
  const pool = ctx.candidate.enrichment.pool;
  if (!pool) return unk('H7', 'Liquidity floor + buy impact', 'pool unavailable');

  const reserveSol = quoteReserveSol(pool);
  const minSol = ctx.config.guardrails.minPoolSol;
  const sizeSol = ctx.config.entry.baseSizeSol;
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
function unk(id: string, label: string, detail: string): CheckResult {
  return { id, label, status: 'unknown', detail };
}
function pct(x: number): string {
  return `${(x * 100).toFixed(1)}%`;
}
