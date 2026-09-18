import { PublicKey } from '@solana/web3.js';
import { PROGRAM_IDS, LAMPORTS_PER_SOL } from '../core/constants.ts';
import { deriveAta } from '../core/ata.ts';
import type { RpcClient } from '../core/rpc.ts';

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
    return { pda: null, ata: null };
  }
}

/**
 * pump.fun bonding-curve account decoder (pre-graduation lane, S1).
 *
 * Layout after the 8-byte Anchor discriminator — VERIFIED against live
 * mainnet curve accounts 2026-09-18 (fresh curve: virtual 30.9 SOL /
 * real 0.9 SOL / complete=0; graduated curve: drained zeros / complete=1;
 * brand-new curve: all-zero reserves / complete=0):
 *
 *   8    virtualTokenReserves  u64   (base units)
 *   16   virtualSolReserves    u64   (lamports; 30 SOL at creation)
 *   24   realTokenReserves     u64   (base units)
 *   32   realSolReserves       u64   (lamports collected)
 *   40   tokenTotalSupply      u64   (base units; 1e9 * 1e6)
 *   48   complete              bool  (1 once migrated)
 *
 * Trailing bytes (creator/fee keys — account runs 125–151 bytes) are opaque
 * to S1 and intentionally undecoded.
 */
const CURVE_OFF = {
  virtualToken: 8,
  virtualSol: 16,
  realToken: 24,
  realSol: 32,
  supply: 40,
  complete: 48,
} as const;

export const BONDING_CURVE_DISCRIMINATOR = '17b7f83760d8ac60';
const MIN_CURVE_LEN = CURVE_OFF.complete + 1;

export interface CurveReserves {
  curveAddress: string;
  virtualTokenReserves: bigint;
  virtualSolReserves: bigint;
  realTokenReserves: bigint;
  realSolReserves: bigint;
  tokenTotalSupply: bigint;
  complete: boolean;
}

export function decodeBondingCurve(
  base64Data: string,
  owner: string,
  curveAddress: string,
): CurveReserves | null {
  if (owner !== PROGRAM_IDS.PUMP_FUN) return null;
  const buf = Buffer.from(base64Data, 'base64');
  if (buf.length < MIN_CURVE_LEN) return null;
  if (buf.subarray(0, 8).toString('hex') !== BONDING_CURVE_DISCRIMINATOR) return null;
  return {
    curveAddress,
    virtualTokenReserves: buf.readBigUInt64LE(CURVE_OFF.virtualToken),
    virtualSolReserves: buf.readBigUInt64LE(CURVE_OFF.virtualSol),
    realTokenReserves: buf.readBigUInt64LE(CURVE_OFF.realToken),
    realSolReserves: buf.readBigUInt64LE(CURVE_OFF.realSol),
    tokenTotalSupply: buf.readBigUInt64LE(CURVE_OFF.supply),
    complete: buf[CURVE_OFF.complete] === 1,
  };
}

/** True when the curve holds no liquidity yet (brand-new) or was drained (migrated). */
export function isEmptyCurve(r: CurveReserves): boolean {
  return r.virtualTokenReserves === 0n || r.virtualSolReserves === 0n;
}

/** Mid price in SOL per base unit from virtual reserves. Null when unpriced. */
export function curvePriceSol(r: CurveReserves): number | null {
  if (isEmptyCurve(r)) return null;
  const price = Number(r.virtualSolReserves) / LAMPORTS_PER_SOL / Number(r.virtualTokenReserves);
  return Number.isFinite(price) && price > 0 ? price : null;
}

/** SOL actually collected on the curve (progress toward graduation). */
export function curveRealSol(r: CurveReserves): number {
  return Number(r.realSolReserves) / LAMPORTS_PER_SOL;
}

/**
 * Read + decode the bonding-curve account for a mint. Returns null when the
 * account is missing, foreign-owned, or fails the discriminator — all
 * expected for non-pump.fun mints, never an exception.
 */
export async function fetchBondingCurve(rpc: RpcClient, mint: string): Promise<CurveReserves | null> {
  const curveAddress = deriveBondingCurvePda(mint);
  if (!curveAddress) return null;
  const acct = await rpc.getAccountInfoBase64(curveAddress, 'processed').catch(() => null);
  if (!acct) return null;
  return decodeBondingCurve(acct.data, acct.owner, curveAddress);
}
