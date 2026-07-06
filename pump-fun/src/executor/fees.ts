import type { RpcClient } from '../core/rpc.ts';
import type { Config } from '../config/schema.ts';

/**
 * Priority fee + Jito tip strategy (Section 7.1). Priority fee targets the p75
 * of recent prioritization fees with a hard cap; the Jito tip is the configured
 * cap (the p50-of-winning-tips input needs the Jito auction API, which is paid —
 * added when live infra lands).
 */

const DEFAULT_PRIORITY_MICROLAMPORTS = 50_000;

export interface FeePlan {
  /** Priority fee in micro-lamports per compute unit. */
  priorityMicroLamports: number;
  /** Jito tip in lamports (0 when Jito is unconfigured). */
  jitoTipLamports: number;
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
    jitoTipLamports: config.jito?.tipCapLamports ?? 0,
  };
}

export function percentile(values: number[], p: number): number {
  if (values.length === 0) return 0;
  const sorted = [...values].sort((a, b) => a - b);
  const idx = Math.min(sorted.length - 1, Math.max(0, Math.ceil((p / 100) * sorted.length) - 1));
  return sorted[idx] ?? 0;
}
