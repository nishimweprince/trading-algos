import { PublicKey } from '@solana/web3.js';
import { PROGRAM_IDS } from '../core/constants.ts';
import { deriveAta } from '../core/ata.ts';

/**
 * pump.fun bonding-curve holdings (H5 input).
 *
 * A graduation moves ~20% of supply from the bonding curve into the pool's
 * base vault. `getTokenLargestAccounts` at `confirmed` commitment lags the
 * `processed` migration by 1–2 slots, so the curve's pre-migration token
 * account still shows up as the largest "holder" — and the pool-vault
 * exclusion does not cover it. Both addresses below are derived locally from
 * the mint (no RPC), so excluding them makes the snapshot timing irrelevant.
 *
 * Seeds verified against stored mainnet graduations: for mint
 * CZ2e6zmofvAM3wUdCSUm6sBrG3KcvtDGfTvU5L4Upump the ["bonding-curve", mint]
 * PDA owns the 20.69% top holder account.
 */

const BONDING_CURVE_SEED = 'bonding-curve';

/** Derive the pump.fun bonding-curve PDA for a mint. Null when underivable. */
export function deriveBondingCurvePda(mint: string): string | null {
  try {
    return PublicKey.findProgramAddressSync(
      [Buffer.from(BONDING_CURVE_SEED), new PublicKey(mint).toBuffer()],
      new PublicKey(PROGRAM_IDS.PUMP_FUN),
    )[0].toBase58();
  } catch {
    return null;
  }
}

/**
 * Addresses to exclude from H5 concentration alongside the pool vaults: the
 * curve PDA itself (owner match — the form observed live) and its associated
 * token account (account match, belt-and-braces). Either field is null when
 * the mint cannot derive it; callers then fall back to current behavior.
 */
export function bondingCurveExclusions(
  mint: string,
  isToken2022: boolean,
): { pda: string | null; ata: string | null } {
  const pda = deriveBondingCurvePda(mint);
  if (!pda) return { pda: null, ata: null };
  try {
    return { pda, ata: deriveAta(pda, mint, isToken2022) };
  } catch {
    return { pda, ata: null };
  }
}
