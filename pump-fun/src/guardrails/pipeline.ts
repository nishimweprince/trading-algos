import type { Config } from '../config/schema.ts';
import type { TypedBus } from '../core/bus.ts';
import type { Repositories } from '../persistence/repositories.ts';
import type { RpcClient } from '../core/rpc.ts';
import type { GraduationEvent } from '../core/types.ts';
import { logger } from '../core/logger.ts';
import { Enricher } from '../enrichment/index.ts';
import type { Candidate, EnrichmentData, ScreenTimings } from '../enrichment/types.ts';
import { GuardrailEngine } from './engine.ts';
import type { RiskManager } from '../risk/manager.ts';
import type { ShadowTracker } from './shadow.ts';
import { computePrice } from '../positions/pricing.ts';
import { momentumSizeFactor } from './scoring.ts';
import { computeEntrySizeSol } from '../config/sizing.ts';
import { extractStrategyFeatures } from '../dashboard/features.ts';
import { getActiveRunSession } from '../core/session.ts';
import { FeatureEngine } from '../enrichment/features/index.ts';
import { ConfirmObserver, evaluateConfirm, type ConfirmObservation } from './confirmGate.ts';
import { PricePoller } from '../positions/pricing.ts';
import { fetchSwaps, flowStats, TX_FLOW_VERSION } from '../enrichment/txFlow.ts';
import type { CandidateVerdict } from '../core/types.ts';
import { MetaModel } from './model.ts';
import type { FeatureInput } from '../research/featureSpec.ts';
import type { EntryDecider, EntryDecision } from '../decision/entryDecider.ts';
import type { EntryStateInput } from '../decision/entryState.ts';
import { FastPoolReader } from './fastRead.ts';
import type { AmmConfigCache, PrefetchedSwapStates } from '../executor/swapState.ts';

/**
 * Screening pipeline — fast path (2026-09-28).
 *
 *   graduation ─► ONE batched account read ─► engine (local) ─► openPosition
 *
 * That is the whole path between detection and the buy request: no indexer,
 * no simulation, no third-party API. Everything else — persisting the verdict,
 * alerts, shadow tracking, the full research enrichment (holders, DAS, early
 * flow, manipulation features), the decision model's shadow call — is queued
 * with setImmediate AFTER the open is dispatched, so none of it can delay a
 * send, and none of it gates one.
 */
export class GuardrailPipeline {
  private readonly config: Config;
  private readonly bus: TypedBus;
  private readonly repos: Repositories;
  private readonly enricher: Enricher;
  private readonly engine: GuardrailEngine;
  private readonly reader: FastPoolReader;
  private readonly risk: RiskManager | undefined;
  private readonly shadow: ShadowTracker | undefined;
  private readonly features: FeatureEngine;
  private readonly rpc: RpcClient;
  private readonly confirmReader: PricePoller;
  private readonly model: MetaModel | null;
  private readonly decision: EntryDecider | undefined;
  private readonly log = logger.child({ mod: 'guardrails' });
  private unsubscribe: (() => void) | null = null;

  constructor(deps: {
    config: Config;
    bus: TypedBus;
    repos: Repositories;
    /** Background research reads (enrichment, features, confirm watch). */
    rpc: RpcClient;
    /** The fast read's own client; defaults to `rpc`. Keep it uncontended. */
    fastRpc?: RpcClient;
    risk?: RiskManager;
    shadow?: ShadowTracker;
    /** Decision model (Jev); absent when decision.provider is none. */
    decision?: EntryDecider;
    /** Trading wallet (dry-run/live): its ATAs join the fast read, and a buy state is prefetched. */
    user?: string;
    swapStates?: PrefetchedSwapStates;
    ammConfigs?: AmmConfigCache;
    /** Start of the launch stream's unbroken coverage window (Detector.launchCoverageSinceMs). */
    launchCoverageSinceMs?: () => number | null;
  }) {
    this.config = deps.config;
    this.bus = deps.bus;
    this.repos = deps.repos;
    this.risk = deps.risk;
    this.shadow = deps.shadow;
    this.decision = deps.decision;
    const g = deps.config.guardrails;
    this.enricher = new Enricher({
      rpc: deps.rpc,
      budgetMs: g.enrichmentBudgetMs,
      holdersRetryDelaysMs: g.holdersNotMintRetryDelaysMs,
      momentumWindowMs: g.momentumWindowMs,
      momentumWindowBucketsMs: g.momentumWindowBucketsMs,
      ...(g.momentumTxStatsEnabled ? { momentumTxStats: { maxTx: g.momentumTxStatsMaxTx } } : {}),
    });
    this.engine = new GuardrailEngine(deps.config, deps.repos, deps.risk, deps.launchCoverageSinceMs);
    this.reader = new FastPoolReader({
      rpc: deps.fastRpc ?? deps.rpc,
      retryDelaysMs: g.fastReadRetryDelaysMs,
      ...(deps.user ? { user: deps.user } : {}),
      ...(deps.swapStates ? { swapStates: deps.swapStates } : {}),
      ...(deps.ammConfigs ? { ammConfigs: deps.ammConfigs } : {}),
      launchCreator: (mint) => {
        try {
          return deps.repos.launchByMint(mint)?.creator ?? null;
        } catch {
          return null;
        }
      },
    });
    this.features = new FeatureEngine({ rpc: deps.rpc, repos: deps.repos, config: g.features });
    this.rpc = deps.rpc;
    this.model = MetaModel.load(deps.config.model.path);
    if (deps.config.model.enabled) {
      // Its inputs (early flow, holders, socials) only exist after the
      // background enrichment, so it can no longer gate the fast path.
      this.log.warn('model.enabled: the learned filter no longer gates entries (fast path) — scored in shadow only');
    }
    if (deps.decision?.mode === 'gate') {
      this.log.warn('decision.mode gate: every accept waits on the decision model (up to decision.timeoutMs) before the buy');
    }
    // Used only for one-shot vault reads during confirm windows; never started.
    this.confirmReader = new PricePoller(deps.rpc, deps.config.entry.confirm.pollMs, undefined, {
      commitment: deps.config.positions.priceCommitment,
    });
  }

  start(): void {
    this.unsubscribe = this.bus.on('graduation', (g) => void this.screen(g));
    this.log.info('guardrail pipeline listening for graduations (fast path)');
  }

  stop(): void {
    this.unsubscribe?.();
    this.unsubscribe = null;
  }

  private async screen(g: GraduationEvent): Promise<void> {
    const screenStarted = Date.now();
    if (this.decision?.mode === 'gate') this.decision.warm();
    try {
      const candidate = await this.fastCandidate(g);
      const verdict = this.engine.evaluate(candidate);
      const gateDecision =
        verdict.verdict === 'accept' && this.decision?.mode === 'gate' && this.decision.eligible(verdict)
          ? await this.decision.applyGate(this.decisionInput(candidate, verdict), verdict)
          : null;
      const timings = candidate.enrichment.timings!;
      timings.verdictMs = Date.now() - screenStarted;
      candidate.enrichment.elapsedMs = timings.verdictMs;

      // The buy goes out first; confirm mode defers it to the confirm phase.
      if (verdict.verdict === 'accept' && this.config.entry.mode !== 'confirm') {
        this.requestOpen(candidate, verdict);
      }
      this.bus.emit('verdict', verdict);
      setImmediate(() => void this.afterVerdict(candidate, verdict, gateDecision, screenStarted));
    } catch (err) {
      this.log.error('screening failed', { mint: g.mint, err });
    }
  }

  /** The candidate the engine sees: one batched account read, nothing else. */
  private async fastCandidate(g: GraduationEvent): Promise<Candidate> {
    const read = await this.reader.read(g);
    const timings: ScreenTimings = {};
    const enrichment: EnrichmentData = { unknowns: [], elapsedMs: 0, timings };
    if (read.ok) {
      const s = read.snapshot;
      enrichment.pool = s.pool;
      if (s.mintInfo) enrichment.mintInfo = s.mintInfo;
      if (s.creatorHolding) enrichment.creatorHolding = s.creatorHolding;
      timings.fastReadMs = s.readMs;
      timings.fastReadAttempts = s.attempts;
      timings.reservesFrom = s.reservesFrom;
    } else {
      // Carried to P0 as its fail reason.
      enrichment.unknowns.push(`pool:${read.reason}`);
      timings.fastReadMs = read.readMs;
      timings.fastReadAttempts = read.attempts;
    }
    return { graduation: g, enrichment };
  }

  /**
   * Emit an openPosition intent for an accepted candidate. Momentum sizing
   * needs early post-graduation flow, which does not exist yet at this point;
   * without it the factor is 1 (base size), exactly as before when the flow
   * sample missed.
   */
  private requestOpen(candidate: Candidate, verdict: CandidateVerdict): void {
    const pool = candidate.enrichment.pool;
    if (!pool) {
      this.log.warn('accepted but no pool to price — skipping open', { mint: candidate.graduation.mint });
      return;
    }
    const g = this.config.guardrails;
    const momentumFactor =
      g.momentumSizeEnabled && candidate.enrichment.earlyFlow
        ? momentumSizeFactor(
            candidate.enrichment.earlyFlow.netInflowSol,
            g.momentumSizeFullInflowSol,
            g.momentumSizeFloorMultiplier,
          )
        : 1;
    const walletSol = this.risk?.getSnapshot()?.walletBalanceSol ?? 0;
    const scoreMultiplier = this.config.entry.scoreSizingEnabled ? verdict.sizeMultiplier : 1;
    const sizeSol = computeEntrySizeSol(
      this.config,
      walletSol,
      scoreMultiplier,
      momentumFactor,
      false,
      verdict.decisionSizeFactor ?? 1,
    );
    if (sizeSol <= 0) {
      this.log.info('accepted but size below entry.minAbsoluteSol — skipping open', {
        mint: candidate.graduation.mint,
        minAbsoluteSol: this.config.entry.minAbsoluteSol,
      });
      this.bus.emit('entryVetoed', { mint: candidate.graduation.mint, reason: 'GUARDRAIL', detail: 'SIZE_BELOW_FLOOR' });
      return;
    }
    const floor = this.config.wallet.balanceFloorSol;
    if (this.config.mode !== 'paper' && walletSol < floor + sizeSol) {
      this.log.warn('accepted but wallet cannot fund this size — skipping open', {
        mint: candidate.graduation.mint,
        walletSol: Number(walletSol.toFixed(4)),
        sizeSol: Number(sizeSol.toFixed(4)),
        floor,
      });
      return;
    }

    this.bus.emit('openPosition', {
      mint: candidate.graduation.mint,
      sizeSol,
      highVolatility: verdict.highVolatility,
      relaxedRisk: false,
      relaxedReasons: [],
      feedSource: candidate.graduation.feedSource,
      venue: candidate.graduation.venue,
      ...(candidate.graduation.detectedAtMs !== undefined ? { detectedAtMs: candidate.graduation.detectedAtMs } : {}),
      entrySoftScore: verdict.softScore,
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
   * Everything that used to sit between detection and the buy, now after it:
   * persistence, alerts, shadow tracking, confirm arms, and the research
   * enrichment. Never gates anything.
   */
  private async afterVerdict(
    candidate: Candidate,
    verdict: CandidateVerdict,
    gateDecision: EntryDecision | null,
    screenStarted: number,
  ): Promise<void> {
    const g = candidate.graduation;
    try {
      this.recordVerdict(candidate, verdict);
      if (gateDecision) this.decision?.persist(g.mint, gateDecision, 'gate');

      const failed = verdict.hardChecks.filter((c) => c.status === 'fail').map((c) => c.id);
      const t = candidate.enrichment.timings;
      this.log.info('verdict', {
        mint: g.mint,
        verdict: verdict.verdict,
        failed,
        vetoReasons: verdict.vetoReasons,
        verdictMs: t?.verdictMs,
        fastReadMs: t?.fastReadMs,
        reads: t?.fastReadAttempts,
        detectToVerdictMs: g.detectedAtMs !== undefined ? Date.now() - g.detectedAtMs : undefined,
      });
      if (verdict.verdict === 'veto') {
        this.bus.emit('entryVetoed', { mint: g.mint, reason: 'GUARDRAIL', detail: verdict.vetoReasons.join(',') });
        this.bus.emit('alert', {
          level: 'info',
          message: `⛔ veto ${short(g.mint)} — ${verdict.vetoReasons.join(', ') || 'guardrail'}`,
        });
        this.shadowTrackVeto(candidate, verdict.vetoReasons, verdict.highVolatility);
      } else {
        this.bus.emit('alert', {
          level: 'info',
          message: `✅ accept ${short(g.mint)} — verdict in ${t?.verdictMs ?? '?'} ms`,
        });
      }
    } catch (err) {
      this.log.error('post-verdict bookkeeping failed', { mint: g.mint, err });
    }

    await Promise.all([
      this.runConfirmPhase(candidate, verdict).catch((err) => this.log.debug('confirm phase failed', { mint: g.mint, err })),
      this.config.guardrails.backgroundEnrichment
        ? this.researchEnrich(candidate, verdict, screenStarted).catch((err) =>
            this.log.debug('research enrichment failed', { mint: g.mint, err }),
          )
        : Promise.resolve(),
    ]);
  }

  private recordVerdict(candidate: Candidate, verdict: CandidateVerdict): void {
    try {
      const features = extractStrategyFeatures(candidate.enrichment, softLike(verdict));
      const session = getActiveRunSession();
      this.repos.recordVerdict(verdict, safeJson(candidate.enrichment), {
        sessionId: session?.id ?? null,
        configHash: session?.configHash ?? null,
        sizeMultiplier: features.sizeMultiplier,
        poolSolAtEntry: features.poolSolAtEntry,
        buyImpactPct: features.buyImpactPct,
        creatorShare: features.creatorShare,
        scoreComponentsJson: features.scoreComponents ? JSON.stringify(features.scoreComponents) : null,
        unknownsJson: features.unknowns.length ? JSON.stringify(features.unknowns) : null,
        enrichmentMs: candidate.enrichment.timings?.verdictMs ?? null,
        relaxedRisk: false,
        relaxedReasonsJson: null,
        creator: features.creator,
        mcapSolAtEntry: features.mcapSolAtEntry,
        populationOk: populationOkFrom(verdict.hardChecks),
        featuresJson: featuresJsonFrom(candidate.enrichment),
      });
    } catch (err) {
      this.log.error('failed to persist verdict', { mint: candidate.graduation.mint, err });
    }
  }

  /**
   * Research data for the verdict row, after the fact: holders, DAS
   * metadata, early flow, manipulation features, then the learned filter and
   * decision model in shadow. The verdict's pool snapshot is kept — it is
   * what the decision was made on.
   */
  private async researchEnrich(candidate: Candidate, verdict: CandidateVerdict, screenStarted: number): Promise<void> {
    const g = candidate.graduation;
    const e = candidate.enrichment;
    const timings = e.timings!;
    this.decision?.warm();
    const momentumP = this.enricher.startMomentum(g);
    const enrichStarted = Date.now();
    const slow = await this.enricher.enrich(g);
    timings.enrichMs = Date.now() - enrichStarted;
    if (slow.enrichment.holders) e.holders = slow.enrichment.holders;
    if (slow.enrichment.metadata) e.metadata = slow.enrichment.metadata;
    if (slow.enrichment.dasAuthorities) e.dasAuthorities = slow.enrichment.dasAuthorities;
    if (slow.enrichment.dasCreators) e.dasCreators = slow.enrichment.dasCreators;
    if (!e.pool && slow.enrichment.pool) e.pool = slow.enrichment.pool;
    if (!e.mintInfo && slow.enrichment.mintInfo) e.mintInfo = slow.enrichment.mintInfo;
    if (e.pool && slow.enrichment.pool?.lpMintSupply !== undefined) e.pool.lpMintSupply = slow.enrichment.pool.lpMintSupply;
    e.unknowns.push(...slow.enrichment.unknowns);
    this.decision?.shadowMetadata(g.mint, e.metadata);

    const featuresStarted = Date.now();
    const [momentum, features] = await Promise.all([
      momentumP.then((m) => {
        timings.momentumMs = Date.now() - screenStarted;
        return Enricher.resolveMomentum(m, e.pool);
      }),
      this.features.enabled
        ? this.features
            .compute(candidate, timings)
            .catch((err) => {
              this.log.debug('manipulation features failed', { mint: g.mint, err });
              return undefined;
            })
            .finally(() => {
              timings.featuresMs = Date.now() - featuresStarted;
            })
        : Promise.resolve(undefined),
    ]);
    if (features) {
      this.features.addEarlyFlowFeatures(candidate, features, momentum.swaps);
      e.features = features;
    }
    e.momentumWindowMs = momentum.momentumWindowMs;
    if (momentum.earlyFlow) e.earlyFlow = momentum.earlyFlow;
    else if (momentum.missed) e.unknowns.push('earlyFlow');
    timings.totalMs = Date.now() - screenStarted;

    const modelScore = this.model ? this.model.score(this.featureInputFor(candidate), this.config.model.minProb) : null;
    const f = extractStrategyFeatures(e, softLike(verdict));
    this.repos.updateCandidateResearch(g.mint, {
      enrichmentJson: safeJson(e),
      earlyFlowNetSol: f.earlyFlowNetSol,
      earlyFlowRate: f.earlyFlowRate,
      top10Share: f.top10Share,
      maxHolderShare: f.maxHolderShare,
      hasSocials: f.hasSocials,
      unknownsJson: f.unknowns.length ? JSON.stringify(f.unknowns) : null,
      momentumWindowMs: f.momentumWindowMs,
      featuresJson: featuresJsonFrom(e),
      ...(modelScore ? { modelVersion: modelScore.version, modelProb: modelScore.prob } : {}),
    });
    if (this.decision?.mode !== 'gate' && this.decision?.eligible(verdict)) {
      this.decision.shadow(g.mint, this.decisionInput(candidate, verdict));
    }
  }

  /** The persisted feature fields, as the learned filter and the decision model read them. */
  private featureInputFor(candidate: Candidate): FeatureInput {
    const e = candidate.enrichment;
    const flow = extractStrategyFeatures(e);
    return {
      earlyFlowNetSol: flow.earlyFlowNetSol,
      earlyFlowRate: flow.earlyFlowRate,
      poolSolAtEntry: flow.poolSolAtEntry,
      top10Share: flow.top10Share,
      maxHolderShare: flow.maxHolderShare,
      creatorShare: flow.creatorShare,
      rugcheckScore: flow.rugcheckScore,
      hasSocials: flow.hasSocials,
      mintAgeMs: flow.mintAgeMs,
      mcapSolAtEntry: flow.mcapSolAtEntry,
      poolMovePct: flow.poolMovePct,
      sellabilityStatus: flow.sellabilityStatus,
      momentumWindowMs: flow.momentumWindowMs,
      featuresJson: featuresJsonFrom(e),
    };
  }

  private decisionInput(candidate: Candidate, verdict: CandidateVerdict): EntryStateInput {
    return {
      ...this.featureInputFor(candidate),
      softScore: verdict.softScore,
      checks: Object.fromEntries(verdict.hardChecks.map((c) => [c.id, c.status])),
    };
  }

  /**
   * Register a vetoed candidate with the shadow dry-run tracker so we can later
   * measure whether the veto was a false positive (full paper exit PnL + peak
   * MFE). No-op when shadow tracking is disabled or the pool couldn't be
   * decoded/priced. Never opens a live position or sends capital.
   */
  private shadowTrackVeto(candidate: Candidate, vetoReasons: string[], highVolatility: boolean): void {
    if (!this.shadow) return;
    this.shadow.noteEligible(candidate.graduation.mint);
    const pool = candidate.enrichment.pool;
    if (!pool) {
      this.shadow.noteSkippedMissingPricing(candidate.graduation.mint);
      return;
    }
    const baseDecimals = candidate.enrichment.mintInfo?.decimals ?? 6;
    const baselinePrice = computePrice(pool.baseReserve, pool.quoteReserveLamports, baseDecimals);
    if (!(baselinePrice > 0)) {
      this.shadow.noteSkippedMissingPricing(candidate.graduation.mint);
      return;
    }
    const session = getActiveRunSession();
    this.shadow.track({
      mint: candidate.graduation.mint,
      verdict: 'veto',
      primaryVetoCode: vetoReasons[0] ?? null,
      vetoCodes: vetoReasons,
      baselinePrice,
      quoteReserveSol: Number(pool.quoteReserveLamports) / 1e9,
      highVolatility,
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

  /**
   * P3.2 confirm phase. One pool watch per graduation serves:
   *  - the live confirm entry (entry.mode = confirm) for accepted candidates;
   *  - the shadow A/B arms (shadow.confirmArmsMs) for every canonical (H12-pass)
   *    graduation, accepted or not — hypothetical delayed entries tracked into
   *    confirm_outcomes, gate-passed and gate-rejected alike, so the gate itself
   *    is measurable.
   */
  private async runConfirmPhase(candidate: Candidate, verdict: CandidateVerdict): Promise<void> {
    const pool = candidate.enrichment.pool;
    if (!pool) return;
    const entryDelay = verdict.verdict === 'accept' && this.config.entry.mode === 'confirm' ? this.config.entry.confirm.delayMs : null;
    const h12 = verdict.hardChecks.find((c) => c.id === 'H12');
    const canonical = !h12 || h12.status === 'pass';
    const arms = this.shadow && canonical ? this.config.shadow.confirmArmsMs : [];
    const delays = [...arms, ...(entryDelay !== null ? [entryDelay] : [])];
    if (!delays.length) return;
    const baseDecimals = candidate.enrichment.mintInfo?.decimals ?? 6;
    const startPrice = computePrice(pool.baseReserve, pool.quoteReserveLamports, baseDecimals);
    if (!(startPrice > 0)) return;
    const observer = new ConfirmObserver({
      read: (ref) => this.confirmReader.readOnce(ref),
      // Arms alone are not latency-critical: poll them at 1 s.
      pollMs: entryDelay !== null ? this.config.entry.confirm.pollMs : 1_000,
    });
    await observer.observe(
      { baseVault: pool.baseVault, quoteVault: pool.quoteVault, baseDecimals },
      { price: startPrice, quoteReserveLamports: pool.quoteReserveLamports },
      delays,
      async (o) => {
        if (arms.includes(o.delayMs)) this.startConfirmArm(candidate, verdict, o);
        if (o.delayMs === entryDelay) await this.confirmEntry(candidate, verdict, o);
      },
    );
  }

  private startConfirmArm(candidate: Candidate, verdict: CandidateVerdict, o: ConfirmObservation): void {
    const pool = candidate.enrichment.pool;
    if (!this.shadow || !pool || !(o.endPrice > 0)) return;
    const decision = evaluateConfirm(o, this.config.entry.confirm);
    const code = decision.ok ? null : `CONFIRM_${decision.reason.toUpperCase()}`;
    const session = getActiveRunSession();
    this.shadow.track({
      mint: candidate.graduation.mint,
      verdict: 'confirm_arm',
      arm: `confirm_${o.delayMs}`,
      primaryVetoCode: code,
      vetoCodes: [...(code ? [code] : []), ...verdict.vetoReasons],
      baselinePrice: o.endPrice,
      quoteReserveSol: Number(pool.quoteReserveLamports) / 1e9,
      highVolatility: verdict.highVolatility,
      poolRef: {
        mint: candidate.graduation.mint,
        baseVault: pool.baseVault,
        quoteVault: pool.quoteVault,
        baseDecimals: candidate.enrichment.mintInfo?.decimals ?? 6,
      },
      sessionId: session?.id ?? null,
      configHash: session?.configHash ?? null,
    });
  }

  private async confirmEntry(candidate: Candidate, verdict: CandidateVerdict, o: ConfirmObservation): Promise<void> {
    const pool = candidate.enrichment.pool!;
    const mint = candidate.graduation.mint;
    const cfg = this.config.entry.confirm;
    let obs = o;
    if (cfg.minUniqueBuyers > 0) {
      try {
        const { swaps } = await fetchSwaps(this.rpc, pool.poolAddress, mint, new Set([pool.poolAddress]), {
          maxTx: this.config.guardrails.momentumTxStatsMaxTx,
          quoteVault: pool.quoteVault,
        });
        obs = { ...o, uniqueBuyers: flowStats(swaps).uniqueBuyers };
      } catch (err) {
        this.log.debug('confirm buyer count unavailable', { mint, err });
      }
    }
    const decision = evaluateConfirm(obs, cfg);
    try {
      this.repos.mergeCandidateFeatures(mint, {
        confirm: {
          delayMs: obs.delayMs,
          ok: decision.ok,
          ...(decision.ok ? {} : { reason: decision.reason }),
          netInflowSol: obs.netInflowSol,
          priceUpPct: obs.priceUpPct,
          maxSingleDropPct: obs.maxSingleDropPct,
          samples: obs.samples,
          ...(obs.uniqueBuyers !== undefined ? { uniqueBuyers: obs.uniqueBuyers } : {}),
        },
      });
    } catch (err) {
      this.log.debug('confirm result persist failed', { mint, err });
    }
    if (!decision.ok) {
      this.bus.emit('entryVetoed', { mint, reason: 'GUARDRAIL', detail: `CONFIRM_FAILED:${decision.reason}` });
      this.log.info('confirm entry rejected', { mint, delayMs: obs.delayMs, reason: decision.reason, detail: decision.detail });
      return;
    }
    // Price the open off the confirm-time pool, not the migration snapshot.
    if (obs.endQuoteReserveLamports > 0n && obs.endBaseReserve > 0n) {
      candidate.enrichment.pool = { ...pool, baseReserve: obs.endBaseReserve, quoteReserveLamports: obs.endQuoteReserveLamports };
    }
    this.log.info('confirm entry passed', { mint, delayMs: obs.delayMs, netInflowSol: obs.netInflowSol, priceUpPct: obs.priceUpPct });
    this.requestOpen(candidate, verdict);
  }
}

function softLike(verdict: CandidateVerdict) {
  return {
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
}

/**
 * One JSON object with the P3 signals the learned filter trains on: early
 * flow (incl. tx stats) and the manipulation features, plus the per-phase
 * screening timings. Null when none of them ran.
 */
export function featuresJsonFrom(e: EnrichmentData): string | null {
  const flow = e.earlyFlow
    ? {
        netInflowSol: e.earlyFlow.netInflowSol,
        inflowRateSolPerSec: e.earlyFlow.inflowRateSolPerSec,
        windowMs: e.earlyFlow.windowMs,
        ...(e.earlyFlow.tx ? { tx: e.earlyFlow.tx, txFlowVersion: TX_FLOW_VERSION } : {}),
      }
    : undefined;
  if (!flow && !e.features && !e.timings) return null;
  return safeJson({
    ...(flow ? { earlyFlow: flow } : {}),
    ...(e.features ? { manipulation: e.features } : {}),
    ...(e.timings ? { timings: e.timings } : {}),
  });
}

/** H12 population check outcome; null when the check did not run (disabled / older configs). */
function populationOkFrom(checks: Array<{ id: string; status: string }>): boolean | null {
  const h12 = checks.find((c) => c.id === 'H12');
  return h12 ? h12.status === 'pass' : null;
}

/** JSON.stringify with bigint support (supply/reserves are bigint). */
function safeJson(value: unknown): string {
  return JSON.stringify(value, (_k, v) => (typeof v === 'bigint' ? v.toString() : v));
}

function short(mint: string): string {
  return mint.length > 10 ? `${mint.slice(0, 4)}…${mint.slice(-4)}` : mint;
}
