import type { PoolRef, PriceRead } from '../positions/pricing.ts';
import { LAMPORTS_PER_SOL } from '../core/constants.ts';

/**
 * Confirmation entry (work plan 2026-09-25 P3.2, F11).
 *
 * The first seconds after a migration are sniper-dominated: holds < 1 s ran
 * WR 32 %, 3–10 s −0.32 SOL, > 30 s WR 69 %. The hypothesis is that the edge
 * is in second-wave entries. Instead of buying on detection, watch the pool
 * for `delayMs` and enter only when the flow confirms:
 *
 *  - net SOL inflow over the window >= minNetInflowSol;
 *  - price above the migration price by [minPriceUpPct, maxPriceUpPct]
 *    (moving, but not already blown out);
 *  - no single tick-to-tick sell took >= maxSingleSellPoolPct of pool SOL;
 *  - optionally >= minUniqueBuyers distinct buyers.
 *
 * One ConfirmObserver poll loop serves several delays at once, so the live
 * gate and every shadow arm (5 / 15 / 30 s) share a single pool watch.
 */
export interface ConfirmCriteria {
  minNetInflowSol: number;
  minPriceUpPct: number;
  maxPriceUpPct: number;
  maxSingleSellPoolPct: number;
  minUniqueBuyers: number;
}

export interface ConfirmObservation {
  delayMs: number;
  samples: number;
  startQuoteSol: number;
  endQuoteSol: number;
  netInflowSol: number;
  /** End price vs the migration (baseline) price, %. */
  priceUpPct: number;
  /** Largest single tick-to-tick quote-reserve drop, % of pool SOL. */
  maxSingleDropPct: number;
  endPrice: number;
  endBaseReserve: bigint;
  endQuoteReserveLamports: bigint;
  uniqueBuyers?: number;
}

export type ConfirmDecision = { ok: true } | { ok: false; reason: string; detail: string };

export function evaluateConfirm(o: ConfirmObservation, c: ConfirmCriteria): ConfirmDecision {
  if (o.samples === 0) return { ok: false, reason: 'no_data', detail: 'no pool reads during the confirm window' };
  if (o.maxSingleDropPct >= c.maxSingleSellPoolPct) {
    return { ok: false, reason: 'large_sell', detail: `single sell took ${o.maxSingleDropPct.toFixed(1)}% of pool SOL` };
  }
  if (o.netInflowSol < c.minNetInflowSol) {
    return { ok: false, reason: 'weak_inflow', detail: `net inflow ${o.netInflowSol.toFixed(2)} SOL < ${c.minNetInflowSol}` };
  }
  if (o.priceUpPct < c.minPriceUpPct) {
    return { ok: false, reason: 'price_below_migration', detail: `price ${o.priceUpPct.toFixed(1)}% vs migration (< ${c.minPriceUpPct}%)` };
  }
  if (o.priceUpPct > c.maxPriceUpPct) {
    return { ok: false, reason: 'blown_out', detail: `price already +${o.priceUpPct.toFixed(1)}% (> ${c.maxPriceUpPct}%)` };
  }
  if (c.minUniqueBuyers > 0 && (o.uniqueBuyers ?? 0) < c.minUniqueBuyers) {
    return { ok: false, reason: 'few_buyers', detail: `${o.uniqueBuyers ?? 0} unique buyers < ${c.minUniqueBuyers}` };
  }
  return { ok: true };
}

export class ConfirmObserver {
  private readonly read: (ref: Pick<PoolRef, 'baseVault' | 'quoteVault' | 'baseDecimals'>) => Promise<PriceRead | null>;
  private readonly sleep: (ms: number) => Promise<void>;
  private readonly now: () => number;
  private readonly pollMs: number;

  constructor(deps: {
    read: ConfirmObserver['read'];
    pollMs: number;
    sleep?: (ms: number) => Promise<void>;
    now?: () => number;
  }) {
    this.read = deps.read;
    this.pollMs = deps.pollMs;
    this.sleep = deps.sleep ?? ((ms) => new Promise((r) => setTimeout(r, ms)));
    this.now = deps.now ?? Date.now;
  }

  /**
   * Watch the pool from now until the largest delay, calling `onDelay` with
   * the window snapshot as each delay elapses (in ascending order).
   */
  async observe(
    ref: Pick<PoolRef, 'baseVault' | 'quoteVault' | 'baseDecimals'>,
    start: { price: number; quoteReserveLamports: bigint },
    delaysMs: readonly number[],
    onDelay: (o: ConfirmObservation) => void | Promise<void>,
  ): Promise<void> {
    const pending = [...new Set(delaysMs)].filter((d) => d >= 0).sort((a, b) => a - b);
    if (!pending.length) return;
    const t0 = this.now();
    const startQuote = start.quoteReserveLamports;
    let prevQuote = startQuote;
    let last: PriceRead = { price: start.price, baseReserve: 0n, quoteReserveLamports: startQuote };
    let samples = 0;
    let maxDrop = 0;

    while (pending.length) {
      const elapsed = this.now() - t0;
      const nextDue = pending[0]!;
      await this.sleep(Math.max(0, Math.min(this.pollMs, nextDue - elapsed)));
      const r = await this.read(ref).catch(() => null);
      if (r && r.price > 0) {
        samples++;
        if (prevQuote > 0n && r.quoteReserveLamports < prevQuote) {
          maxDrop = Math.max(maxDrop, (Number(prevQuote - r.quoteReserveLamports) / Number(prevQuote)) * 100);
        }
        prevQuote = r.quoteReserveLamports;
        last = r;
      }
      const now = this.now() - t0;
      while (pending.length && now >= pending[0]!) {
        const delayMs = pending.shift()!;
        await onDelay({
          delayMs,
          samples,
          startQuoteSol: Number(startQuote) / LAMPORTS_PER_SOL,
          endQuoteSol: Number(last.quoteReserveLamports) / LAMPORTS_PER_SOL,
          netInflowSol: Number(last.quoteReserveLamports - startQuote) / LAMPORTS_PER_SOL,
          priceUpPct: start.price > 0 ? (last.price / start.price - 1) * 100 : 0,
          maxSingleDropPct: maxDrop,
          endPrice: last.price,
          endBaseReserve: last.baseReserve,
          endQuoteReserveLamports: last.quoteReserveLamports,
        });
      }
    }
  }
}
