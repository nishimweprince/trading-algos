/**
 * Adversarial exit conditions (paper-honesty Unit 9): bundle dumps, Token-2022
 * transfer fees, and non-canonical pools.
 *
 * Base legs assume a clean exit. The `{leg}_adv` legs reprice the same exits
 * after the bundled supply dumps ahead of us, minus the Token-2022 transfer
 * fee when one exists. They derive from BASE legs only (`_adj` and `_adv`
 * are alternative views — expectancy vs adversarial-conditional — never
 * stacked).
 *
 * - Dump: the migrate-time bundle (bundledTokens, raw units) is assumed to
 *   sell into the exit pool ahead of us. Our fill reprices against the
 *   post-dump quote; a drained pool scores −100 minus costs. The bundle is
 *   the migrate-time snapshot — later accumulation is invisible, so this is
 *   a lower bound on dump pressure, stated not hidden.
 * - T22 fee: withheld from our token sale, min(proceeds × bps, maxFee).
 *   Tristate: absent on SPL (no fee possible), null on T22 proven clean,
 *   'unknown' when the mint was never read — unknown fees NULL the `_adv`
 *   legs (t22_fee_unknown), never zero them.
 * - Non-canonical pool: the base vault sits somewhere other than the mint's
 *   canonical PDA, weakening the P0-by-construction premise (a non-standard
 *   pool could carry freeze/hook powers the program guarantee excludes).
 *   `_adv` legs NULL (non_canonical_pool). Legacy tracks with no verdict
 *   fail open — the backfill derives it offline.
 *
 * Freeze gets no haircut: pump-created mints revoke the freeze authority at
 * creation (the program guarantee live P0 relies on), so modeling freeze
 * losses would invent risk the live bot also ignores. The non-canonical
 * guard above is the freeze hook: it fires exactly where the guarantee
 * stops holding.
 */
import { PublicKey } from '@solana/web3.js';
import { canonicalPumpPoolPda } from '@pump-fun/pump-swap-sdk';
import { PROGRAM_IDS } from '../core/constants.ts';
import { sellProceedsSol } from '../positions/ammImpact.ts';
import type { PoolMark } from './economics.ts';

/** Pump.fun token decimals, SPL and Token-2022 alike. */
export const PUMP_TOKEN_DECIMALS = 6;

export interface TransferFee {
  bps: number;
  maxRaw: bigint;
}

/** Entry-feature fields the adversarial legs read (structural; EntryFeatureSnapshot satisfies it). */
export interface AdvFeatures {
  bundledTokens?: string | null;
  transferFee?: TransferFeeWire | null;
  poolCanonical?: boolean | null;
  tokenProgram?: string | null;
}

/** Wire form (entry_features_json): bigint as decimal string; 'unknown' = T22 mint never read. */
export type TransferFeeWire = { bps: number; maxRaw: string } | 'unknown';

export type AdvBlockReason = 'non_canonical_pool' | 'bundle_unknown' | 't22_fee_unknown';

export type AdvContext =
  | { blocked: true; reason: AdvBlockReason }
  | { blocked: false; reason: null; bundledRaw: bigint; transferFee: TransferFee | null };

/**
 * Resolve the track-level adversarial context from entry features. Unknown
 * inputs block (fail open): an unknown bundle is not a zero bundle, and an
 * unknown program cannot rule out a transfer fee. Explicit zeros price.
 */
export function advContextFromFeatures(features: AdvFeatures | null | undefined): AdvContext {
  const blocked = (reason: AdvBlockReason): AdvContext => ({ blocked: true, reason });
  if (features?.poolCanonical === false) return blocked('non_canonical_pool');
  if (features?.bundledTokens === undefined || features?.bundledTokens === null) return blocked('bundle_unknown');
  let bundledRaw = 0n;
  try {
    bundledRaw = BigInt(features.bundledTokens);
  } catch {
    return blocked('bundle_unknown');
  }
  if (bundledRaw < 0n) return blocked('bundle_unknown');
  let transferFee: TransferFee | null = null;
  const program = features.tokenProgram;
  if (program === undefined || program === null) return blocked('t22_fee_unknown');
  if (program === PROGRAM_IDS.TOKEN_2022) {
    const tf = features.transferFee;
    // null = proven clean (read ok, no extension); 'unknown'/absent = never verified.
    if (tf === undefined || tf === 'unknown') return blocked('t22_fee_unknown');
    if (tf) transferFee = { bps: tf.bps, maxRaw: BigInt(tf.maxRaw) };
  }
  return { blocked: false, reason: null, bundledRaw, transferFee };
}

/**
 * The exit pool after the bundle dumps ahead of us. `drained` = nothing left
 * to sell into — the vault is empty (or the quote was already corrupt) — so
 * the leg scores −100. Constant-product math alone never drains the quote,
 * but the dump can drain the vault the quote props up.
 */
export function dumpedExit(
  exit: PoolMark,
  bundledRaw: bigint,
  decimals: number = PUMP_TOKEN_DECIMALS,
): { exit: PoolMark; drained: boolean } {
  if (!(bundledRaw > 0n) || !(exit.price > 0)) return { exit, drained: false };
  const bundleNotionalSol = (Number(bundledRaw) / 10 ** decimals) * exit.price;
  if (!(bundleNotionalSol > 0) || !Number.isFinite(bundleNotionalSol)) return { exit, drained: false };
  const dumpTakesSol = sellProceedsSol(bundleNotionalSol, exit.quoteSol);
  const quoteSol = exit.quoteSol - dumpTakesSol;
  const realQuoteSol = exit.realQuoteSol === undefined ? undefined : Math.max(0, exit.realQuoteSol - dumpTakesSol);
  const drained = !(quoteSol > 0) || realQuoteSol === 0;
  if (drained) return { exit: { ...exit, quoteSol: Math.max(0, quoteSol), ...(realQuoteSol === undefined ? {} : { realQuoteSol }) }, drained: true };
  return { exit: { ...exit, quoteSol, ...(realQuoteSol === undefined ? {} : { realQuoteSol }) }, drained: false };
}

/** Transfer fee on exit proceeds of `proceedsSol`, as % of size. */
export function t22FeePct(
  proceedsSol: number,
  exitPrice: number,
  fee: TransferFee,
  sizeSol: number,
  decimals: number = PUMP_TOKEN_DECIMALS,
): number {
  if (!(proceedsSol > 0) || !(sizeSol > 0)) return 0;
  const proportional = proceedsSol * (Math.max(0, fee.bps) / 10_000);
  const capSol = (Number(fee.maxRaw) / 10 ** decimals) * exitPrice;
  const feeSol = capSol > 0 ? Math.min(proportional, capSol) : proportional;
  return (feeSol / sizeSol) * 100;
}

/** Whether the pool sits at the mint's canonical PDA (null = mint undecodable, fail open). */
export function isCanonicalPool(mint: string, poolAddress: string | null): boolean | null {
  if (!poolAddress) return null;
  try {
    return canonicalPumpPoolPda(new PublicKey(mint)).toBase58() === poolAddress;
  } catch {
    return null;
  }
}
