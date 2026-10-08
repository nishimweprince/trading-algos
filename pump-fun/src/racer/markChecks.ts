/**
 * Migrate-mark validation for racer paper (pure).
 *
 * Two observed failure shapes, one rule: a rejected open is NULL with a
 * reason, never a default (paperRoundTripPct and the horizons aggregation
 * both skip null legs).
 *
 * 1. Ratio collapse (forward risk): the migrate-tx `open` is backed out of
 *    the caller's bundled buy (open = reach·ratio²). Misattributed
 *    `bundledTokens` collapse the ratio and the open prints ~1000x low.
 *    Caught at write time by checkOpenMark (drain bound).
 * 2. Legacy constant opens (observed 2026-10-05): pre-v2 tracks carry an
 *    unparsed constant open/reach (4.1e-7/85 SOL — the right LEVEL for a
 *    fresh pool, but identical across mints) while their vault-observed slot
 *    marks sit ~3500x away. Neither side is corroborated, so any migrate leg
 *    disagreeing with the row's own slot marks is excluded. Legacy rows never
 *    entered the verified medians; the flags keep analysts from tripping on
 *    them in raw exports.
 */
import type { PoolMark } from './economics.ts';

export type OpenRejectReason = 'non-positive' | 'open-above-reach' | 'implausible-drain' | 'slot-drift';

/** Bundled buy draining more than this share of the pre-buy base is misattribution, not a trade. */
export const DEFAULT_MAX_DRAIN_SHARE = 0.75;
/** An open disagreeing with the first slot mark by more than this factor is a derivation error. */
export const DEFAULT_MAX_SLOT_DRIFT = 20;

export interface OpenCheckOpts {
  maxDrainShare?: number;
  maxSlotDrift?: number;
}

export interface OpenCheck {
  open: PoolMark | null;
  reason: OpenRejectReason | null;
}

function drainShare(bundledTokens: bigint, poolBase: bigint): number | null {
  if (bundledTokens <= 0n || poolBase <= 0n) return null;
  const b = Number(bundledTokens);
  const p = Number(poolBase);
  if (!Number.isFinite(b) || !Number.isFinite(p)) return null;
  return b / (p + b);
}

/**
 * Entry-time check: needs only the migrate tx itself. A null open (unknown by
 * derivation) passes through as unknown — rejection is only for marks that
 * claim a value and fail it. The drain check is skipped when open == reach
 * (no bundled buy: the open IS the reach, nothing was backed out).
 */
export function checkOpenMark(
  open: PoolMark | null,
  reach: PoolMark,
  bundledTokens: bigint,
  poolBase: bigint,
  opts: OpenCheckOpts = {},
): OpenCheck {
  if (!open) return { open: null, reason: null };
  if (!(open.price > 0) || !Number.isFinite(open.price)) return { open: null, reason: 'non-positive' };
  if (open.price > reach.price) return { open: null, reason: 'open-above-reach' };
  if (open.price !== reach.price) {
    const drain = drainShare(bundledTokens, poolBase);
    const max = opts.maxDrainShare ?? DEFAULT_MAX_DRAIN_SHARE;
    if (drain !== null && drain > max) return { open: null, reason: 'implausible-drain' };
  }
  return { open, reason: null };
}

/**
 * Slot cross-check once the realistic entries exist: the open must agree with
 * the first slot mark within `maxSlotDrift`× either way. Null-safe — when
 * either side is missing there is nothing to contradict.
 */
export function checkOpenSlotDrift(open: PoolMark | null, firstSlotMid: number | null, opts: OpenCheckOpts = {}): OpenRejectReason | null {
  if (!open || !(firstSlotMid !== null && firstSlotMid > 0 && Number.isFinite(firstSlotMid))) return null;
  const max = opts.maxSlotDrift ?? DEFAULT_MAX_SLOT_DRIFT;
  const ratio = open.price / firstSlotMid;
  if (!(ratio > 0) || !Number.isFinite(ratio)) return 'non-positive';
  return ratio > max || ratio < 1 / max ? 'slot-drift' : null;
}

/**
 * Positive-evidence disagreement between one migrate-tx mark and the row's own
 * vault-observed slot marks. Fail-open: when either side is missing there is
 * nothing to contradict. Per-leg, because a ratio bug breaks the open while
 * the direct-read reach stays honest. Shared by the worker (stage 2) and the
 * query layer so legacy and new rows filter identically.
 */
export function markSlotDisagree(markPrice: number | null, firstSlotMid: number | null, maxSlotDrift = DEFAULT_MAX_SLOT_DRIFT): boolean {
  if (!(markPrice !== null && markPrice > 0 && Number.isFinite(markPrice))) return false;
  if (!(firstSlotMid !== null && firstSlotMid > 0 && Number.isFinite(firstSlotMid))) return false;
  const r = markPrice / firstSlotMid;
  return r > maxSlotDrift || r < 1 / maxSlotDrift;
}

/** Either migrate leg disagrees with the row's slot marks. */
export function migrateSlotDisagree(
  openPrice: number | null,
  reachPrice: number | null,
  firstSlotMid: number | null,
  maxSlotDrift = DEFAULT_MAX_SLOT_DRIFT,
): boolean {
  return markSlotDisagree(openPrice, firstSlotMid, maxSlotDrift) || markSlotDisagree(reachPrice, firstSlotMid, maxSlotDrift);
}

/**
 * Query-side guard for STORED (open, reach) pairs, including legacy rows that
 * predate per-track flags: the same drain bound, expressed through the mark
 * ratio (open/reach = ratio², so a 0.75 drain floor is 0.0625). Shared with
 * the horizons aggregation so old and new rows filter identically.
 */
export function isPlausibleStoredOpen(openPrice: number | null, reachPrice: number | null, maxDrainShare = DEFAULT_MAX_DRAIN_SHARE): boolean {
  if (openPrice === null || reachPrice === null) return false;
  if (!(openPrice > 0) || !(reachPrice > 0) || !Number.isFinite(openPrice) || !Number.isFinite(reachPrice)) return false;
  if (openPrice > reachPrice) return false;
  const floor = (1 - maxDrainShare) * (1 - maxDrainShare);
  return openPrice / reachPrice >= floor;
}
