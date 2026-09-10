import type { Config } from './schema.ts';

/**
 * Convert a wallet-% knob into SOL, never below the dust floor.
 * A missing/zero wallet (paper tests, unread balance) yields minAbsoluteSol.
 */
export function sizeFromWalletPct(walletSol: number, pct: number, minAbsoluteSol: number): number {
  const raw = Math.max(0, walletSol) * (pct / 100);
  return Math.max(raw, minAbsoluteSol);
}

export interface EntrySizeLadder {
  minSol: number;
  baseSol: number;
  maxSol: number;
  relaxedMaxSol: number;
}

/** SOL rungs implied by the current wallet balance and percent knobs. */
export function entrySizeLadder(config: Config, walletSol: number): EntrySizeLadder {
  const e = config.entry;
  const minSol = sizeFromWalletPct(walletSol, e.minSizeWalletPct, e.minAbsoluteSol);
  const baseSol = sizeFromWalletPct(walletSol, e.baseSizeWalletPct, e.minAbsoluteSol);
  const maxSol = Math.max(sizeFromWalletPct(walletSol, e.maxSizeWalletPct, e.minAbsoluteSol), minSol);
  const relaxedMaxSol = sizeFromWalletPct(
    walletSol,
    config.guardrails.relaxedRiskMaxSizeWalletPct,
    e.minAbsoluteSol,
  );
  return { minSol, baseSol, maxSol, relaxedMaxSol };
}

/**
 * Final position size: score × momentum on the base rung, clamped to [min, max].
 * Relaxed-risk skips the min floor and is hard-capped at relaxedMaxSol.
 */
export function computeEntrySizeSol(
  config: Config,
  walletSol: number,
  sizeMultiplier: number,
  momentumFactor: number,
  relaxedRisk: boolean,
): number {
  const { minSol, baseSol, maxSol, relaxedMaxSol } = entrySizeLadder(config, walletSol);
  let sizeSol = Math.min(baseSol * sizeMultiplier * momentumFactor, maxSol);
  if (relaxedRisk) {
    sizeSol = Math.min(sizeSol, relaxedMaxSol);
  } else if (sizeSol < minSol) {
    sizeSol = Math.min(minSol, maxSol);
  }
  return sizeSol;
}
