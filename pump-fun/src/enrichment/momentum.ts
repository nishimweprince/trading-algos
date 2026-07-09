import type { RpcClient } from '../core/rpc.ts';
import { LAMPORTS_PER_SOL } from '../core/constants.ts';
import { decodeTokenAccountAmount } from './pool.ts';
import { logger } from '../core/logger.ts';

/**
 * Post-graduation early-flow momentum (Section 6.2 soft signal). The structural
 * guardrails say nothing about whether anyone is *buying* — but the strategy
 * bets on a fast +50% move, so early buying pressure is a real signal.
 *
 * We measure it the same way the pricing layer measures price: from the pool's
 * own quote (SOL) vault, never an external API. Sampling the vault at graduation
 * and again a few seconds later gives the net SOL added to the pool over the
 * window (buys add SOL, sells remove it) — a directional, controllable proxy
 * for early momentum. The rate (SOL/sec) additionally flags a fast-filling pool
 * as high-volatility, which tightens the trailing stop (exits/engine.ts).
 */

export interface EarlyFlow {
  /** Quote (SOL) vault balance at the start of the window, lamports. */
  quoteReserveStartLamports: bigint;
  /** Quote (SOL) vault balance at the end of the window, lamports. */
  quoteReserveEndLamports: bigint;
  /** Net SOL added to the pool over the window (buys − sells). May be negative. */
  netInflowSol: number;
  /** Window actually observed, ms (may differ slightly from the requested one). */
  windowMs: number;
  /** Net SOL inflow per second — the momentum rate. */
  inflowRateSolPerSec: number;
}

/** Pure derivation of the early-flow metrics from two vault readings. */
export function computeEarlyFlow(
  startLamports: bigint,
  endLamports: bigint,
  windowMs: number,
): EarlyFlow {
  const netInflowSol = Number(endLamports - startLamports) / LAMPORTS_PER_SOL;
  const seconds = windowMs / 1000;
  const inflowRateSolPerSec = seconds > 0 ? netInflowSol / seconds : 0;
  return {
    quoteReserveStartLamports: startLamports,
    quoteReserveEndLamports: endLamports,
    netInflowSol,
    windowMs,
    inflowRateSolPerSec,
  };
}

export interface MomentumSamplerDeps {
  rpc: RpcClient;
  /** Injectable clock/sleep for deterministic tests. */
  now?: () => number;
  sleep?: (ms: number) => Promise<void>;
}

/**
 * Samples a pool's quote vault twice — once at registration time (caller passes
 * the graduation reserve) and once after `windowMs` — and derives the early
 * flow. Sampling is best-effort: a missing vault returns null so enrichment can
 * mark the signal unknown rather than fail.
 */
export class MomentumSampler {
  private readonly rpc: RpcClient;
  private readonly now: () => number;
  private readonly sleep: (ms: number) => Promise<void>;
  private readonly log = logger.child({ mod: 'momentum' });

  constructor(deps: MomentumSamplerDeps) {
    this.rpc = deps.rpc;
    this.now = deps.now ?? (() => Date.now());
    this.sleep = deps.sleep ?? ((ms) => new Promise((resolve) => setTimeout(resolve, ms)));
  }

  /**
   * Wait out the window, re-read the quote vault, and return the early flow.
   * `startLamports` is the vault balance observed at graduation (already fetched
   * during pool decode), so only one extra RPC read is needed.
   */
  async sample(quoteVault: string, startLamports: bigint, windowMs: number): Promise<EarlyFlow | null> {
    if (windowMs <= 0) return null;
    const startedMs = this.now();
    await this.sleep(windowMs);
    const acct = await this.rpc.getAccountInfoBase64(quoteVault);
    if (!acct) {
      this.log.debug('early-flow sample missed — quote vault unavailable', { quoteVault });
      return null;
    }
    const endLamports = decodeTokenAccountAmount(acct.data);
    // Use the observed elapsed time (>=1ms) so the rate reflects reality even if
    // the sleep overran; never divide by zero.
    const observedMs = Math.max(1, this.now() - startedMs);
    return computeEarlyFlow(startLamports, endLamports, observedMs);
  }
}
