/**
 * Sanitized config fingerprint for strategy-week attribution.
 * Secrets and interpolated RPC URLs with tokens must never land in SQLite.
 */
import { createHash } from 'node:crypto';
import { execSync } from 'node:child_process';
import type { Config } from '../config/schema.ts';

/** Trading knobs only — no hostnames that may embed API keys. */
export function sanitizeConfigForAnalytics(config: Config): Record<string, unknown> {
  return {
    mode: config.mode,
    entry: {
      minSizeWalletPct: config.entry.minSizeWalletPct,
      baseSizeWalletPct: config.entry.baseSizeWalletPct,
      maxSizeWalletPct: config.entry.maxSizeWalletPct,
      minAbsoluteSol: config.entry.minAbsoluteSol,
      maxSlippagePct: config.entry.maxSlippagePct,
      mode: config.entry.mode,
      buyRetrySlippageTiers: config.entry.buyRetrySlippageTiers,
      maxEntryMovePct: config.entry.maxEntryMovePct ?? null,
      scoreSizingEnabled: config.entry.scoreSizingEnabled,
    },
    guardrails: {
      creatorHoldingsCapPct: config.guardrails.creatorHoldingsCapPct,
      minPoolSol: config.guardrails.minPoolSol,
      maxBuyImpactPct: config.guardrails.maxBuyImpactPct,
      creatorMaxLaunches7d: config.guardrails.creatorMaxLaunches7d,
      enrichmentBudgetMs: config.guardrails.enrichmentBudgetMs,
      momentumWindowMs: config.guardrails.momentumWindowMs,
      momentumWindowBucketsMs: config.guardrails.momentumWindowBucketsMs,
      momentumStrongInflowSol: config.guardrails.momentumStrongInflowSol,
      momentumMaxScoreBonus: config.guardrails.momentumMaxScoreBonus,
      highVolInflowRateSolPerSec: config.guardrails.highVolInflowRateSolPerSec,
      // Work plan 2026-09-25 P2: every risk-policy knob must move the hash,
      // otherwise two different experiments share one config_hash stratum.
      clusterVeto: config.guardrails.features.enabled && config.guardrails.features.cluster.enabled && config.guardrails.features.cluster.veto,
      clusterWarm: { ...config.guardrails.clusterWarm },
      relaxedRiskSizeMultiplierCap: config.guardrails.relaxedRiskSizeMultiplierCap,
      relaxedRiskMaxSizeWalletPct: config.guardrails.relaxedRiskMaxSizeWalletPct,
      relaxedRiskMaxOpenPositions: config.guardrails.relaxedRiskMaxOpenPositions,
      relaxedRiskEmergencyLpDropPct: config.guardrails.relaxedRiskEmergencyLpDropPct,
      population: { ...config.guardrails.population },
      momentumSizeEnabled: config.guardrails.momentumSizeEnabled,
      momentumSizeFullInflowSol: config.guardrails.momentumSizeFullInflowSol,
      momentumSizeFloorMultiplier: config.guardrails.momentumSizeFloorMultiplier,
    },
    exits: { ...config.exits },
    risk: {
      maxConcurrentPositions: config.risk.maxConcurrentPositions,
      dailyLossLimitSol: config.risk.dailyLossLimitSol,
      dailyLossLimitWalletPct: config.risk.dailyLossLimitWalletPct,
      consecutiveLossHalt: config.risk.consecutiveLossHalt,
      consecutiveLossHaltMinutes: config.risk.consecutiveLossHaltMinutes,
      emergencyExitCount24h: config.risk.emergencyExitCount24h,
      edgeMonitor: { ...config.risk.edgeMonitor },
    },
    fees: { ...config.fees },
    // P4.1 execution path: a different send route / CU policy is a different
    // experiment (fills and latency move). The API-key env var name is omitted.
    heliusSender: {
      enabled: config.heliusSender.enabled,
      swqosOnly: config.heliusSender.swqosOnly,
      minTipLamports: config.heliusSender.minTipLamports,
      tipCapLamports: config.heliusSender.tipCapLamports,
      tipPercentile: config.heliusSender.tipPercentile,
      tipBufferPct: config.heliusSender.tipBufferPct,
    },
    execution: {
      dynamicComputeUnits: config.execution.dynamicComputeUnits,
      skipBuySimulate: config.execution.skipBuySimulate,
    },
    simulator: { ...config.simulator, entryHaircutPct: { ...config.simulator.entryHaircutPct } },
    wallet: { balanceFloorSol: config.wallet.balanceFloorSol },
    detector: {
      pumpportalEnabled: config.detector.pumpportalEnabled,
      heliusWsEnabled: config.detector.heliusWsEnabled,
      heliusAtlasEnabled: config.detector.heliusAtlasEnabled,
      laserstreamEnabled: config.detector.laserstreamEnabled,
      laserstreamLaunchesEnabled: config.detector.laserstreamLaunchesEnabled,
      confirmOnChain: config.detector.confirmOnChain,
    },
  };
}

export function configHash(sanitized: Record<string, unknown>): string {
  const canonical = JSON.stringify(sanitized);
  return createHash('sha256').update(canonical).digest('hex').slice(0, 16);
}

export function tryGitCommit(): string | null {
  try {
    const out = execSync('git rev-parse --short HEAD', {
      encoding: 'utf8',
      stdio: ['ignore', 'pipe', 'ignore'],
      timeout: 1000,
    }).trim();
    return out || null;
  } catch {
    return null;
  }
}
