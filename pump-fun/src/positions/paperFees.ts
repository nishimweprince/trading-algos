/**
 * Paper / shadow fee drag (same formula the position manager uses for non-live
 * accounting). Priority+tip per tx (entry + each exit fill) and swap fee on both
 * legs of the round-trip notional.
 */
export function estimatePaperFees(
  sizeSol: number,
  exitFills: number,
  fees: { swapFeePct: number; estPriorityTipSolPerTx: number },
): number {
  const txCount = 1 + Math.max(1, exitFills);
  const tip = fees.estPriorityTipSolPerTx * txCount;
  const swap = (fees.swapFeePct / 100) * sizeSol * 2; // entry + exit legs
  return tip + swap;
}
