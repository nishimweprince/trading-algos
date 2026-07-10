import type { Config } from '../config/schema.ts';
import type { TypedBus } from '../core/bus.ts';
import type { Repositories } from '../persistence/repositories.ts';
import type { RpcClient } from '../core/rpc.ts';
import type { GraduationEvent } from '../core/types.ts';
import { logger, registerSecret } from '../core/logger.ts';
import { readSecret } from '../config/load.ts';
import { Enricher } from '../enrichment/index.ts';
import { GuardrailEngine } from './engine.ts';
import type { SellabilitySimulator } from '../executor/sellability.ts';
import type { RiskManager } from '../risk/manager.ts';
import type { ShadowTracker } from './shadow.ts';
import { computePrice } from '../positions/pricing.ts';
import { extractStrategyFeatures } from '../dashboard/features.ts';
import { getActiveRunSession } from '../core/session.ts';

/**
 * Screening pipeline (Phase 2). Subscribes to `graduation`, enriches the
 * candidate, runs the guardrail engine, persists the verdict with full check
 * results (the raw material for threshold tuning, Section 10), and emits the
 * verdict / veto onto the bus.
 */
export class GuardrailPipeline {
  private readonly config: Config;
  private readonly bus: TypedBus;
  private readonly repos: Repositories;
  private readonly enricher: Enricher;
  private readonly engine: GuardrailEngine;
  private readonly sellability: SellabilitySimulator | undefined;
  private readonly risk: RiskManager | undefined;
  private readonly shadow: ShadowTracker | undefined;
  private readonly log = logger.child({ mod: 'guardrails' });
  private unsubscribe: (() => void) | null = null;

  constructor(deps: {
    config: Config;
    bus: TypedBus;
    repos: Repositories;
    rpc: RpcClient;
    sellability?: SellabilitySimulator;
    risk?: RiskManager;
    shadow?: ShadowTracker;
  }) {
    this.config = deps.config;
    this.bus = deps.bus;
    this.repos = deps.repos;
    this.sellability = deps.sellability;
    this.risk = deps.risk;
    this.shadow = deps.shadow;
    // RugCheck advisory signal — opt-in; the API key (higher rate limits) is
    // read from env and registered for log redaction.
    let rugcheck: { apiKey?: string } | undefined;
    if (deps.config.guardrails.rugcheckEnabled) {
      const apiKey = readSecret(deps.config.guardrails.rugcheckApiKeyEnvVar);
      if (apiKey) registerSecret(apiKey);
      rugcheck = apiKey ? { apiKey } : {};
      this.log.info('rugcheck enabled', { authenticated: Boolean(apiKey) });
    }

    this.enricher = new Enricher({
      rpc: deps.rpc,
      budgetMs: deps.config.guardrails.enrichmentBudgetMs,
      momentumWindowMs: deps.config.guardrails.momentumWindowMs,
      momentumWindowBucketsMs: deps.config.guardrails.momentumWindowBucketsMs,
      ...(rugcheck ? { rugcheck } : {}),
    });
    this.engine = new GuardrailEngine(deps.config, deps.repos, deps.risk);
  }

  start(): void {
    this.unsubscribe = this.bus.on('graduation', (g) => void this.screen(g));
    this.log.info('guardrail pipeline listening for graduations');
  }

  stop(): void {
    this.unsubscribe?.();
    this.unsubscribe = null;
  }

  /**
   * Emit an openPosition intent for an accepted candidate. Requires the decoded
   * pool (for pricing); an accept without a pool can only happen in paper mode
   * (unknowns tolerated) and is skipped with a log rather than mispriced.
   */
  private requestOpen(candidate: Awaited<ReturnType<Enricher['enrich']>>, sizeMultiplier: number, highVolatility: boolean): void {
    const pool = candidate.enrichment.pool;
    if (!pool) {
      this.log.warn('accepted but no pool to price — skipping open', { mint: candidate.graduation.mint });
      return;
    }
    const sizeSol = Math.min(
      this.config.entry.baseSizeSol * sizeMultiplier,
      this.config.entry.maxSizeSol,
    );
    if (sizeSol <= 0) return;

    this.bus.emit('openPosition', {
      mint: candidate.graduation.mint,
      sizeSol,
      highVolatility,
      ...(candidate.enrichment.momentumWindowMs !== undefined
        ? { momentumWindowMs: candidate.enrichment.momentumWindowMs }
        : {}),
      pricing: {
        poolAddress: pool.poolAddress,
        baseMint: pool.baseMint,
        baseVault: pool.baseVault,
        quoteVault: pool.quoteVault,
        baseDecimals: candidate.enrichment.mintInfo?.decimals ?? 6,
        baseReserve: pool.baseReserve,
        quoteReserveLamports: pool.quoteReserveLamports,
        creator: pool.coinCreator,
        baseIsToken2022: candidate.enrichment.mintInfo?.isToken2022 ?? false,
      },
    });
  }

  /**
   * Register a vetoed candidate with the shadow tracker so we can later measure
   * whether the veto was a false positive (did the token pump anyway?). No-op
   * when shadow tracking is disabled or the pool couldn't be decoded/priced.
   */
  private shadowTrackVeto(
    candidate: Awaited<ReturnType<Enricher['enrich']>>,
    primaryVetoCode: string | null,
  ): void {
    if (!this.shadow) return;
    const pool = candidate.enrichment.pool;
    if (!pool) return; // nothing to price
    const baseDecimals = candidate.enrichment.mintInfo?.decimals ?? 6;
    const baselinePrice = computePrice(pool.baseReserve, pool.quoteReserveLamports, baseDecimals);
    if (!(baselinePrice > 0)) return;
    const session = getActiveRunSession();
    this.shadow.track({
      mint: candidate.graduation.mint,
      verdict: 'veto',
      primaryVetoCode,
      baselinePrice,
      poolRef: {
        mint: candidate.graduation.mint,
        baseVault: pool.baseVault,
        quoteVault: pool.quoteVault,
        baseDecimals,
      },
      sessionId: session?.id ?? null,
      configHash: session?.configHash ?? null,
    });
  }

  private async screen(g: GraduationEvent): Promise<void> {
    try {
      const candidate = await this.enricher.enrich(g);

      // H4 sellability probe (dry-run/live with a funded wallet). Runs before the
      // engine so checkSellability can read the result. Best-effort.
      if (this.sellability && candidate.enrichment.pool) {
        try {
          candidate.enrichment.sellable = await this.sellability.check(
            candidate.enrichment.pool.poolAddress,
            candidate.enrichment.pool.baseReserve,
            candidate.enrichment.pool.quoteReserveLamports,
          );
        } catch (err) {
          this.log.debug('sellability probe failed', { mint: g.mint, err });
        }
      }

      // Fresh wallet balance for the floor / pct-of-wallet breaker checks.
      await this.risk?.refreshWalletBalance();

      const verdict = this.engine.evaluate(candidate);

      try {
        const softLike = {
          score: verdict.softScore,
          highVolatility: verdict.highVolatility,
          sizeMultiplier: verdict.sizeMultiplier,
          components: verdict.scoreComponents ?? {
            baseline: 0,
            authorities: 0,
            cleanMint: 0,
            socials: 0,
            nameSymbol: 0,
            rugcheck: 0,
            momentum: 0,
          },
        };
        const features = extractStrategyFeatures(candidate.enrichment, softLike);
        const session = getActiveRunSession();
        // Creator share from hard-check detail if present is best-effort; holders snapshot is primary.
        const creatorCheck = verdict.hardChecks.find((c) => c.id === 'H6');
        if (creatorCheck?.detail) {
          const m = /([\d.]+)%/.exec(creatorCheck.detail);
          if (m?.[1]) features.creatorShare = Number(m[1]) / 100;
        }
        this.repos.recordVerdict(verdict, safeJson(candidate.enrichment), {
          sessionId: session?.id ?? null,
          configHash: session?.configHash ?? null,
          sizeMultiplier: features.sizeMultiplier,
          earlyFlowNetSol: features.earlyFlowNetSol,
          earlyFlowRate: features.earlyFlowRate,
          poolSolAtEntry: features.poolSolAtEntry,
          buyImpactPct: features.buyImpactPct,
          top10Share: features.top10Share,
          maxHolderShare: features.maxHolderShare,
          creatorShare: features.creatorShare,
          rugcheckScore: features.rugcheckScore,
          hasSocials: features.hasSocials,
          scoreComponentsJson: features.scoreComponents ? JSON.stringify(features.scoreComponents) : null,
          unknownsJson: features.unknowns.length ? JSON.stringify(features.unknowns) : null,
          enrichmentMs: features.enrichmentMs,
          momentumWindowMs: features.momentumWindowMs,
        });
      } catch (err) {
        this.log.error('failed to persist verdict', { mint: g.mint, err });
      }

      this.bus.emit('verdict', verdict);

      const failed = verdict.hardChecks.filter((c) => c.status === 'fail').map((c) => c.id);
      const unknown = verdict.hardChecks.filter((c) => c.status === 'unknown').map((c) => c.id);

      this.log.info('verdict', {
        mint: g.mint,
        verdict: verdict.verdict,
        score: verdict.softScore,
        sizeMultiplier: verdict.sizeMultiplier,
        failed,
        unknown,
        vetoReasons: verdict.vetoReasons,
        enrichMs: candidate.enrichment.elapsedMs,
      });

      if (verdict.verdict === 'veto') {
        this.bus.emit('entryVetoed', { mint: g.mint, reason: 'GUARDRAIL', detail: verdict.vetoReasons.join(',') });
        this.bus.emit('alert', {
          level: 'info',
          message: `⛔ veto ${short(g.mint)} — ${verdict.vetoReasons.join(', ') || 'guardrail'} (score ${verdict.softScore})`,
        });
        this.shadowTrackVeto(candidate, verdict.vetoReasons[0] ?? null);
      } else {
        this.bus.emit('alert', {
          level: 'info',
          message: `✅ accept ${short(g.mint)} — score ${verdict.softScore}, size×${verdict.sizeMultiplier.toFixed(2)}`,
        });
        this.requestOpen(candidate, verdict.sizeMultiplier, verdict.highVolatility);
      }
    } catch (err) {
      this.log.error('screening failed', { mint: g.mint, err });
    }
  }
}

/** JSON.stringify with bigint support (supply/reserves are bigint). */
function safeJson(value: unknown): string {
  return JSON.stringify(value, (_k, v) => (typeof v === 'bigint' ? v.toString() : v));
}

function short(mint: string): string {
  return mint.length > 10 ? `${mint.slice(0, 4)}…${mint.slice(-4)}` : mint;
}
