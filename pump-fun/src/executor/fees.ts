import type { RpcClient } from '../core/rpc.ts';
import type { Config } from '../config/schema.ts';

/**
 * Priority fee + Jito tip strategy (Section 7.1). Priority fee targets the p75
 * of recent prioritization fees with a hard cap; the Jito tip is the configured
 * cap (the p50-of-winning-tips input needs the Jito auction API, which is paid —
 * added when live infra lands).
 */

const LAMPORTS_PER_SOL = 1_000_000_000;

export interface FeePlan {
  /** Priority fee in micro-lamports per compute unit. */
  priorityMicroLamports: number;
  /**
   * Tip in lamports: to Helius Sender's tip accounts when heliusSender is
   * enabled, else to Jito's (0 when neither is configured). Name kept for the
   * existing call sites.
   */
  jitoTipLamports: number;
}

interface TipFloorRow {
  ema_landed_tips_50th_percentile?: number;
  landed_tips_50th_percentile?: number;
  landed_tips_75th_percentile?: number;
  landed_tips_95th_percentile?: number;
}

export async function buildFeePlan(
  rpc: RpcClient,
  config: Config,
  capMicroLamports = config.fees.priorityCapMicroLamports,
): Promise<FeePlan> {
  const floor = config.fees.priorityFloorMicroLamports;
  let priority = floor;
  try {
    // Prefer the Helius account-aware estimate (medium level); fall back to the
    // getRecentPrioritizationFees p75 when it is disabled or unavailable (e.g.
    // non-Helius endpoints, devnet where the method is disabled).
    const estimate = config.fees.useHeliusFeeEstimate ? await rpc.getPriorityFeeEstimate?.() : undefined;
    if (typeof estimate === 'number' && estimate > 0) {
      priority = Math.max(floor, estimate);
    } else {
      const fees = (await rpc.getRecentPrioritizationFees()).filter((f) => f > 0);
      // Never bid below the configured floor — a p75 that undershoots the floor
      // during a quiet slot would leave an exit too cheap to land promptly.
      if (fees.length > 0) priority = Math.max(floor, percentile(fees, 75));
    }
  } catch {
    // Fall back to the floor; never block a trade on fee telemetry.
  }
  return {
    priorityMicroLamports: Math.min(priority, capMicroLamports),
    jitoTipLamports: config.heliusSender.enabled ? await buildSenderTipLamports(config) : await buildJitoTipLamports(config),
  };
}

/**
 * Helius Sender tip (P4.1). SWQoS-only routing is not an auction: the
 * documented minimum is the whole price. Max routing rides Jito, so it bids
 * the landed-tip percentile plus a buffer, clamped to [min, cap].
 */
export async function buildSenderTipLamports(config: Config, fetchImpl: typeof fetch = fetch): Promise<number> {
  const h = config.heliusSender;
  const clamp = (l: number) => Math.min(h.tipCapLamports, Math.max(h.minTipLamports, l));
  if (h.swqosOnly) return clamp(h.minTipLamports);
  try {
    const res = await fetchImpl(h.tipFloorUrl);
    if (!res.ok) return clamp(h.minTipLamports);
    const row = ((await res.json()) as TipFloorRow[])[0];
    const sol =
      h.tipPercentile === 95
        ? row?.landed_tips_95th_percentile
        : h.tipPercentile === 75
          ? row?.landed_tips_75th_percentile
          : (row?.ema_landed_tips_50th_percentile ?? row?.landed_tips_50th_percentile);
    if (typeof sol !== 'number' || !Number.isFinite(sol) || sol <= 0) return clamp(h.minTipLamports);
    // Round lamports first: 0.0015e9 x 1.1 is 1650000.0000000002 in floats
    // and a bare ceil would overbid by a lamport.
    return clamp(Math.ceil(Math.round(sol * LAMPORTS_PER_SOL * (100 + h.tipBufferPct)) / 100));
  } catch {
    return clamp(h.minTipLamports);
  }
}

async function buildJitoTipLamports(config: Config): Promise<number> {
  if (!config.jito) return 0;
  const fallback = clampTip(config.jito.fallbackTipLamports, config);
  try {
    const res = await fetch(config.jito.tipFloorUrl);
    if (!res.ok) return fallback;
    const rows = (await res.json()) as TipFloorRow[];
    const row = rows[0];
    const sol = row?.ema_landed_tips_50th_percentile ?? row?.landed_tips_50th_percentile;
    if (typeof sol !== 'number' || !Number.isFinite(sol) || sol <= 0) return fallback;
    return clampTip(Math.ceil(sol * LAMPORTS_PER_SOL), config);
  } catch {
    return fallback;
  }
}

function clampTip(lamports: number, config: Config): number {
  const jito = config.jito;
  if (!jito) return 0;
  return Math.min(jito.tipCapLamports, Math.max(jito.minTipLamports, lamports));
}

export function percentile(values: number[], p: number): number {
  if (values.length === 0) return 0;
  const sorted = [...values].sort((a, b) => a - b);
  const idx = Math.min(sorted.length - 1, Math.max(0, Math.ceil((p / 100) * sorted.length) - 1));
  return sorted[idx] ?? 0;
}
