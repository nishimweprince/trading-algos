/**
 * Constant-product AMM price impact for honest shadow fills.
 *
 * Pure math only — pool fees stay charged by the tiered fee model, so this
 * module never adds fees.
 *
 * A buy of `sizeSol` into a pool holding `quoteSol` pushes the execution
 * price to `mid * (1 + sizeSol / quoteSol)`. Selling `valueAtMidSol` back
 * through the same curve yields `valueAtMidSol / (1 + valueAtMidSol /
 * quoteSol)`, which is always strictly less than `quoteSol`: you can never
 * withdraw more SOL than the pool holds.
 */
export function buyFillPrice(mid: number, sizeSol: number, quoteSol: number): number {
  if (!(mid > 0) || !(sizeSol > 0)) return mid;
  if (!(quoteSol > 0)) return mid;
  return mid * (1 + sizeSol / quoteSol);
}

/** SOL proceeds from selling `valueAtMidSol` into a `quoteSol` pool. Always < quoteSol. */
export function sellProceedsSol(valueAtMidSol: number, quoteSol: number): number {
  if (!(valueAtMidSol > 0)) return 0;
  if (!(quoteSol > 0)) return valueAtMidSol;
  return valueAtMidSol / (1 + valueAtMidSol / quoteSol);
}
