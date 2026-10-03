import type { Repositories } from './repositories.ts';

export interface WalletReconciliation {
  available: boolean;
  anchor: { sol: number; at: string } | null;
  latest: { sol: number; at: string } | null;
  /** Wallet SOL now minus at the anchor snapshot. */
  walletDeltaSol: number;
  /** Closed live trades since the anchor: wallet-true where booked, else modelled. */
  closedPnlSol: number;
  /** The same trades on the old modelled basis (what the dashboard used to show). */
  closedModelPnlSol: number;
  closedCount: number;
  closedMissingActuals: number;
  /** SOL sitting in open positions opened since the anchor (intended size). */
  openCostSol: number;
  openCount: number;
  /** ATA rent locked by buys (negative) and reclaimed by the sweeper (positive). */
  rentSol: number;
  explainedSol: number;
  /** walletDelta − explained: SOL that moved with no trade or rent to explain it. */
  unexplainedSol: number;
}

/**
 * Cash reconciliation: does the ledger explain what the wallet did?
 *   explained = closed PnL − cost of still-open positions + rent movements
 * Anchored on the first balance snapshot at/after `sinceIso` (default: the
 * first snapshot ever recorded, i.e. when this accounting went live).
 */
export function computeWalletReconciliation(repos: Repositories, sinceIso: string | null = null): WalletReconciliation {
  const r = repos.walletReconciliation(sinceIso);
  const sol = (l: number) => l / 1e9;
  if (!r.anchor || !r.latest) {
    return {
      available: false, anchor: null, latest: r.latest ? { sol: sol(r.latest.lamports), at: r.latest.createdAt } : null,
      walletDeltaSol: 0, closedPnlSol: 0, closedModelPnlSol: 0, closedCount: 0, closedMissingActuals: 0,
      openCostSol: 0, openCount: 0, rentSol: 0, explainedSol: 0, unexplainedSol: 0,
    };
  }
  const walletDeltaSol = sol(r.latest.lamports - r.anchor.lamports);
  const rentSol = sol(r.rentLamports);
  const explainedSol = r.closedWalletPnlSol - r.openCostSol + rentSol;
  return {
    available: true,
    anchor: { sol: sol(r.anchor.lamports), at: r.anchor.createdAt },
    latest: { sol: sol(r.latest.lamports), at: r.latest.createdAt },
    walletDeltaSol,
    closedPnlSol: r.closedWalletPnlSol,
    closedModelPnlSol: r.closedModelPnlSol,
    closedCount: r.closedCount,
    closedMissingActuals: r.closedMissingActuals,
    openCostSol: r.openCostSol,
    openCount: r.openCount,
    rentSol,
    explainedSol,
    unexplainedSol: walletDeltaSol - explainedSol,
  };
}
