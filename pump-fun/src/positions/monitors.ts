/**
 * In-position emergency monitors (Section 6.4). Runs on every price tick (it
 * rides the poller's existing getMultipleAccounts, so it costs no extra RPC) and
 * fires an emergency signal that maps to EMERGENCY_EXIT — a worst-slippage exit,
 * distinct from the ordinary price stop-loss.
 *
 * - LP_PULL: the pool's quote (SOL) reserve drops sharply from its recent high,
 *   i.e. liquidity is being drained. Detected even when the price math still
 *   looks survivable.
 * - CREATOR_DUMP: the dev's base-token balance falls well below its first
 *   observed level — the most common death mechanism.
 */

import type { Config } from '../config/schema.ts';
import type { PoolPricingRef } from '../core/types.ts';
import { deriveAta } from '../core/ata.ts';

export interface EmergencySignal {
  kind: 'LP_PULL' | 'CREATOR_DUMP';
  detail: string;
}

export interface EmergencyMonitorConfig {
  /** Quote-reserve drop from window-max that fires LP_PULL, as a percent. */
  lpDropPct: number;
  /** Number of recent ticks forming the rolling window for the LP-pull high-water. */
  windowTicks: number;
  /** Whether the creator-dump check is enabled. */
  creatorDumpEnabled: boolean;
  /** Creator base-balance drop from baseline that fires CREATOR_DUMP, as a percent. */
  creatorDumpPct: number;
}

export interface EmergencyTick {
  quoteReserveLamports: bigint;
  creatorBaseBalance?: bigint;
}

export class EmergencyMonitor {
  private readonly cfg: EmergencyMonitorConfig;
  private readonly window: bigint[] = [];
  private creatorBaseline: bigint | null = null;

  constructor(cfg: EmergencyMonitorConfig) {
    this.cfg = cfg;
  }

  onTick(t: EmergencyTick): EmergencySignal | null {
    // --- LP pull: drop from the rolling window max ---
    this.window.push(t.quoteReserveLamports);
    if (this.window.length > this.cfg.windowTicks) this.window.shift();
    const windowMax = this.window.reduce((m, v) => (v > m ? v : m), 0n);
    if (windowMax > 0n && t.quoteReserveLamports < windowMax) {
      const dropPct = (Number(windowMax - t.quoteReserveLamports) / Number(windowMax)) * 100;
      if (dropPct >= this.cfg.lpDropPct) {
        return { kind: 'LP_PULL', detail: `SOL reserve -${dropPct.toFixed(1)}% from window high` };
      }
    }

    // --- Creator dump: drop from first observed balance ---
    if (this.cfg.creatorDumpEnabled && t.creatorBaseBalance !== undefined) {
      if (this.creatorBaseline === null) {
        this.creatorBaseline = t.creatorBaseBalance;
      } else if (this.creatorBaseline > 0n && t.creatorBaseBalance < this.creatorBaseline) {
        const soldPct = (Number(this.creatorBaseline - t.creatorBaseBalance) / Number(this.creatorBaseline)) * 100;
        if (soldPct >= this.cfg.creatorDumpPct) {
          return { kind: 'CREATOR_DUMP', detail: `creator sold ${soldPct.toFixed(1)}% of holdings` };
        }
      }
    }

    return null;
  }
}

/**
 * Monitor thresholds for a position, tightened for relaxed-risk accepts.
 * Shared by the live manager and the dry-run twin so both legs defend a
 * position with identical rules — otherwise a rug the twin "survives" would be
 * booked as execution drag on the live leg.
 */
export function monitorCfgFor(config: Config, relaxedRisk: boolean): EmergencyMonitorConfig {
  return {
    lpDropPct: relaxedRisk
      ? Math.min(config.exits.emergencyLpDropPct, config.guardrails.relaxedRiskEmergencyLpDropPct)
      : config.exits.emergencyLpDropPct,
    windowTicks: config.exits.lpDropWindowTicks,
    creatorDumpEnabled: config.exits.creatorDumpEnabled,
    creatorDumpPct: config.exits.creatorDumpThresholdPct,
  };
}

/**
 * Creator's base-token ATA to watch for dev-dump, or undefined when the
 * monitor is off, the pool has no creator, or derivation fails (the caller logs
 * that case — the position simply runs without the creator leg).
 */
export function creatorAtaFor(config: Config, pricing: PoolPricingRef): string | undefined {
  if (!config.exits.creatorDumpEnabled || !pricing.creator) return undefined;
  return deriveAta(pricing.creator, pricing.baseMint, pricing.baseIsToken2022 ?? false);
}
