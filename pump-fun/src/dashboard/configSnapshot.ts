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
      baseSizeSol: config.entry.baseSizeSol,
      maxSizeSol: config.entry.maxSizeSol,
      maxSlippagePct: config.entry.maxSlippagePct,
      minEntryScore: config.entry.minEntryScore,
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
    wallet: { balanceFloorSol: config.wallet.balanceFloorSol },
    detector: {
      pumpportalEnabled: config.detector.pumpportalEnabled,
      heliusWsEnabled: config.detector.heliusWsEnabled,
      grpcEnabled: config.detector.grpcEnabled,
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
