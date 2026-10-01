import type { HolderInfo } from './types.ts';
import { BURN_OWNERS, PROTOCOL_SINKS, type PoolInfo } from './pool.ts';
import { bondingCurveExclusions } from './curve.ts';

type HolderLike = Pick<HolderInfo, 'account' | 'owner' | 'share'>;

export interface EffectiveHolderShares<H extends HolderLike = HolderInfo> {
  /** Holders left after removing the pool vaults, bonding curve, burn and protocol sinks. */
  real: H[];
  top10Share: number;
  maxShare: number;
}

/**
 * Holder concentration as H5 judges it. `HolderSnapshot.top10Share`/`maxShare`
 * are RAW: the pool base vault (~20.7 % of supply after migration) is always
 * the largest "holder". H5, the relaxed-risk tag and the persisted
 * top10_share / max_holder_share columns all read this one definition so they
 * cannot drift apart (2026-09-28: the columns stored the raw values, so every
 * row showed max ≈ 0.2068 while H5 saw ~4 %).
 *
 * Needs the decoded pool to know which accounts are vaults; null without it.
 */
export function effectiveHolderShares<H extends HolderLike>(
  holders: { holders: readonly H[] },
  pool: Pick<PoolInfo, 'baseVault' | 'quoteVault'> | undefined,
  mint: string,
  isToken2022: boolean,
): EffectiveHolderShares<H> | null {
  if (!pool) return null;
  const excludedAccounts = new Set([pool.baseVault, pool.quoteVault]);
  // Pre-migration bonding-curve holding (~20% of supply) still visible when
  // the holders snapshot lags the migration: derived locally, no RPC (see
  // enrichment/curve.ts). Owner match is the form observed live; the ATA
  // account match is belt-and-braces.
  const curve = bondingCurveExclusions(mint, isToken2022);
  if (curve.ata) excludedAccounts.add(curve.ata);
  const real = holders.holders.filter(
    (h) =>
      !excludedAccounts.has(h.account) &&
      !(curve.pda && h.owner === curve.pda) &&
      !(h.owner && (BURN_OWNERS.has(h.owner) || PROTOCOL_SINKS.has(h.owner))),
  );
  return {
    real,
    top10Share: real.slice(0, 10).reduce((s, h) => s + h.share, 0),
    maxShare: real[0]?.share ?? 0,
  };
}
