/**
 * Paper size from the real wallet balance (pure). The racer never sends, but
 * its paper tracks and SOL-denominated metrics should reflect what the loaded
 * wallet could actually trade — not a fixed 0.1 SOL regardless of balance.
 *
 * Sizing mirrors the live wallet rules: nothing trades below
 * `wallet.balanceFloorSol`, and an open must leave `wallet.gasHeadroomSol`
 * behind. Above that the paper size is capped at `paperSizeSol` (the proposed
 * trade size, so funded history stays comparable) and floored at
 * `minPaperSizeSol` (dust sizes would let the fixed live cost dominate).
 *
 * Tracks are never skipped for sizing: an unreadable balance falls back to
 * the max, and a below-floor wallet still tracks at the min — both flagged
 * by reason so the dashboard can separate them from funded tracks.
 */

export type WalletSizingReason =
  /** Funded: balance covers the full proposed size. */
  | 'ok'
  /** Funded but small: the balance, not the config, set the size. */
  | 'sized-by-balance'
  /** Balance under the floor (or under floor + headroom + min): tracks at min, flagged. */
  | 'below-floor'
  /** No wallet pubkey configured: the max, flagged. */
  | 'no-wallet'
  /** Balance read failed: the max, flagged. */
  | 'balance-unreadable';

export interface WalletSizing {
  sizeSol: number;
  walletBalanceSol: number | null;
  reason: WalletSizingReason;
}

export interface WalletSizingOpts {
  floorSol: number;
  headroomSol: number;
  minSol: number;
  maxSol: number;
  /** False when no wallet pubkey is configured at all. */
  walletConfigured: boolean;
}

export function paperSizeFromBalance(balanceSol: number | null, opts: WalletSizingOpts): WalletSizing {
  const { floorSol, headroomSol, minSol, maxSol, walletConfigured } = opts;
  if (!walletConfigured) return { sizeSol: maxSol, walletBalanceSol: null, reason: 'no-wallet' };
  if (!(typeof balanceSol === 'number' && Number.isFinite(balanceSol) && balanceSol >= 0)) {
    return { sizeSol: maxSol, walletBalanceSol: null, reason: 'balance-unreadable' };
  }
  const available = balanceSol - floorSol - headroomSol;
  if (balanceSol < floorSol || available < minSol) {
    return { sizeSol: minSol, walletBalanceSol: balanceSol, reason: 'below-floor' };
  }
  if (available < maxSol) return { sizeSol: available, walletBalanceSol: balanceSol, reason: 'sized-by-balance' };
  return { sizeSol: maxSol, walletBalanceSol: balanceSol, reason: 'ok' };
}
