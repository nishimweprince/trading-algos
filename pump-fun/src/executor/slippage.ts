/**
 * Pump AMM custom error 6004 — quote moved past the min-out bound between
 * `swapSolanaState` and simulation. Live buys rebuild with a looser bound
 * instead of aborting after the first 5% simulate.
 */
export const PUMP_AMM_EXCEEDED_SLIPPAGE = 6004;

const DEFAULT_BUY_RETRY_TIERS = [10, 25];

/** True when a simulation / program error is Pump AMM ExceededSlippage. */
export function isExceededSlippage(err: unknown): boolean {
  if (err == null) return false;
  if (hasCustomCode(err, PUMP_AMM_EXCEEDED_SLIPPAGE)) return true;
  return /ExceededSlippage/i.test(flattenErr(err));
}

/**
 * Entry attempts: configured max first, then any looser ladder rungs (typically
 * the exit ladder's 10 / 25). Never tries a tighter bound than maxSlippagePct.
 */
export function buySlippageAttempts(
  maxSlippagePct: number,
  extraTiers: readonly number[] = DEFAULT_BUY_RETRY_TIERS,
): number[] {
  const extras = [...new Set(extraTiers.filter((n) => Number.isFinite(n) && n > maxSlippagePct))].sort(
    (a, b) => a - b,
  );
  return [maxSlippagePct, ...extras];
}

export async function withSlippageRetry<T extends { simErr?: unknown; sent?: boolean }>(
  attempts: readonly number[],
  run: (slippagePct: number) => Promise<T>,
  opts?: { onRetry?: (nextPct: number, prev: T) => void },
): Promise<T> {
  const pcts = attempts.length > 0 ? attempts : DEFAULT_BUY_RETRY_TIERS;
  let last: T | undefined;
  for (let i = 0; i < pcts.length; i++) {
    const pct = pcts[i]!;
    last = await run(pct);
    const retryable = !last.sent && isExceededSlippage(last.simErr);
    if (!retryable || i === pcts.length - 1) return last;
    opts?.onRetry?.(pcts[i + 1]!, last);
  }
  return last!;
}

function hasCustomCode(err: unknown, code: number): boolean {
  if (!err || typeof err !== 'object') return false;
  const ie = (err as { InstructionError?: unknown }).InstructionError;
  if (Array.isArray(ie) && ie[1] && typeof ie[1] === 'object') {
    const custom = (ie[1] as { Custom?: unknown }).Custom;
    if (custom === code) return true;
  }
  for (const v of Object.values(err as Record<string, unknown>)) {
    if (v && typeof v === 'object' && hasCustomCode(v, code)) return true;
  }
  return false;
}

function flattenErr(err: unknown): string {
  if (err instanceof Error) return `${err.name} ${err.message}`;
  if (typeof err === 'string') return err;
  try {
    return JSON.stringify(err) ?? '';
  } catch {
    return String(err);
  }
}

/** Pool reserves at one moment — enough to price the mid. */
export interface ReserveSnapshot {
  baseReserve: bigint;
  quoteReserveLamports: bigint;
}

/**
 * Mid-price move (%) from the enrichment/verdict snapshot to the reserves the
 * live buy is being quoted against. The screening pass and the H4 probe see
 * the pool ~0.5 s after migration; the buy is built ~1–2 s after, inside the
 * sniper window, and the 2026-09-18 first live entry filled at 1.325× the
 * enrichment mid and retraced 19% within 1.5 s. Positive = pool is higher now.
 */
export function entryMovePct(reference: ReserveSnapshot, current: ReserveSnapshot): number | undefined {
  if (reference.baseReserve <= 0n || reference.quoteReserveLamports <= 0n) return undefined;
  if (current.baseReserve <= 0n || current.quoteReserveLamports <= 0n) return undefined;
  const refMid = Number(reference.quoteReserveLamports) / Number(reference.baseReserve);
  const curMid = Number(current.quoteReserveLamports) / Number(current.baseReserve);
  if (!(refMid > 0) || !Number.isFinite(curMid)) return undefined;
  return (curMid / refMid - 1) * 100;
}

export class EntryMoveExceeded extends Error {
  override name = 'EntryMoveExceeded';
  readonly movePct: number;
  readonly capPct: number;
  constructor(movePct: number, capPct: number) {
    super(`pool mid moved ${movePct >= 0 ? '+' : ''}${movePct.toFixed(1)}% since screening > entry.maxEntryMovePct ${capPct}% — not buying the spike`);
    this.movePct = movePct;
    this.capPct = capPct;
  }
}
