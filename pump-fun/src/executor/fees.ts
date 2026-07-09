import type { RpcClient } from '../core/rpc.ts';
import type { Config } from '../config/schema.ts';

/**
 * Priority fee + Jito tip strategy (Section 7.1). Priority fee targets the p75
 * of recent prioritization fees with a hard cap; the Jito tip is the configured
 * cap (the p50-of-winning-tips input needs the Jito auction API, which is paid —
 * added when live infra lands).
 */

const DEFAULT_PRIORITY_MICROLAMPORTS = 50_000;
const LAMPORTS_PER_SOL = 1_000_000_000;

export interface FeePlan {
  /** Priority fee in micro-lamports per compute unit. */
  priorityMicroLamports: number;
  /** Jito tip in lamports (0 when Jito is unconfigured). */
  jitoTipLamports: number;
}

interface TipFloorRow {
  ema_landed_tips_50th_percentile?: number;
  landed_tips_50th_percentile?: number;
}

export async function buildFeePlan(
  rpc: RpcClient,
  config: Config,
  capMicroLamports = 1_000_000,
): Promise<FeePlan> {
  let priority = DEFAULT_PRIORITY_MICROLAMPORTS;
  try {
    const fees = (await rpc.getRecentPrioritizationFees()).filter((f) => f > 0);
    if (fees.length > 0) priority = percentile(fees, 75);
  } catch {
    // Fall back to the default; never block a trade on fee telemetry.
  }
  return {
    priorityMicroLamports: Math.min(priority, capMicroLamports),
    jitoTipLamports: await buildJitoTipLamports(config),
  };
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
