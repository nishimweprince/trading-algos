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
      minEntryScore: config.entry.minEntryScore,
      buyRetrySlippageTiers: config.entry.buyRetrySlippageTiers,
      maxEntryMovePct: config.entry.maxEntryMovePct ?? null,
      scoreSizingEnabled: config.entry.scoreSizingEnabled,
    },
    guardrails: {
      top10HolderCapPct: config.guardrails.top10HolderCapPct,
      singleHolderCapPct: config.guardrails.singleHolderCapPct,
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
      rugcheckEnabled: config.guardrails.rugcheckEnabled,
      // Work plan 2026-09-25 P2: every risk-policy knob must move the hash,
      // otherwise two different experiments share one config_hash stratum.
      tolerateTxTooLargeSellability: config.guardrails.tolerateTxTooLargeSellability,
      tolerateInconclusiveSellability: config.guardrails.tolerateInconclusiveSellability,
      tolerateUnprobedSellability: config.guardrails.tolerateUnprobedSellability,
      tolerateUnknownWhenNoHardFail: config.guardrails.tolerateUnknownWhenNoHardFail,
      sellabilityBuyOnlyBackstop: config.guardrails.sellabilityBuyOnlyBackstop,
      sellabilityProbeSlippagePct: config.guardrails.sellabilityProbeSlippagePct,
      maxProbeMovePct: config.guardrails.maxProbeMovePct ?? null,
      strictTop10HolderCapPct: config.guardrails.strictTop10HolderCapPct,
      strictCreatorHoldingsCapPct: config.guardrails.strictCreatorHoldingsCapPct,
      strictMinPoolSol: config.guardrails.strictMinPoolSol,
      relaxedRiskEnabled: config.guardrails.relaxedRiskEnabled,
      relaxedRiskMaxReasons: config.guardrails.relaxedRiskMaxReasons,
      relaxedRiskSizeMultiplierCap: config.guardrails.relaxedRiskSizeMultiplierCap,
      relaxedRiskMaxSizeWalletPct: config.guardrails.relaxedRiskMaxSizeWalletPct,
      relaxedRiskMaxOpenPositions: config.guardrails.relaxedRiskMaxOpenPositions,
      relaxedRiskEmergencyLpDropPct: config.guardrails.relaxedRiskEmergencyLpDropPct,
      population: { ...config.guardrails.population },
      tokenAgeEnabled: config.guardrails.tokenAgeEnabled,
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
    },
    fees: { ...config.fees },
    simulator: { ...config.simulator, entryHaircutPct: { ...config.simulator.entryHaircutPct } },
    wallet: { balanceFloorSol: config.wallet.balanceFloorSol },
    detector: {
      pumpportalEnabled: config.detector.pumpportalEnabled,
      heliusWsEnabled: config.detector.heliusWsEnabled,
      heliusAtlasEnabled: config.detector.heliusAtlasEnabled,
      laserstreamEnabled: config.detector.laserstreamEnabled,
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
