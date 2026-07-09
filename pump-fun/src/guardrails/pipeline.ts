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
  private readonly log = logger.child({ mod: 'guardrails' });
  private unsubscribe: (() => void) | null = null;

  constructor(deps: {
    config: Config;
    bus: TypedBus;
    repos: Repositories;
    rpc: RpcClient;
    sellability?: SellabilitySimulator;
    risk?: RiskManager;
  }) {
    this.config = deps.config;
    this.bus = deps.bus;
    this.repos = deps.repos;
    this.sellability = deps.sellability;
    this.risk = deps.risk;
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
        this.repos.recordVerdict(verdict, safeJson(candidate.enrichment));
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
