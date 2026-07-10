import type { Config } from '../config/schema.ts';
import type { TypedBus } from '../core/bus.ts';
import type { Repositories } from '../persistence/repositories.ts';
import type { Mint, Position, PoolPricingRef, ExitTrigger } from '../core/types.ts';
import { LAMPORTS_PER_SOL } from '../core/constants.ts';
import { deriveAta } from '../core/ata.ts';
import { logger } from '../core/logger.ts';
import { PaperPosition, type Fill } from './position.ts';
import { computePrice, type PricePoller, type PriceTick } from './pricing.ts';
import { EmergencyMonitor, type EmergencyMonitorConfig } from './monitors.ts';
import type { Executor } from '../executor/index.ts';
import type { BroadcastResult } from '../executor/broadcaster.ts';
import type { ExitLadder } from './presign.ts';
import { ExitSupervisor, parseExitIntent, type ExitOutcome } from './exitSupervisor.ts';
import type { StrategyFeatureFields } from '../persistence/repositories.ts';
import { getActiveRunSession } from '../core/session.ts';

/**
 * Position manager (Section 7.3). In paper mode it opens a simulated position
 * for each accepted candidate, drives the exit FSM off local price ticks, and
 * records fee-adjusted PnL. Concurrency is capped here (a minimal guard; the
 * full risk manager — daily loss, consecutive losses, kill switch — is Phase 5).
 *
 * The live executor (Phase 4) will replace the simulated entry/exit fills with
 * real transactions behind this same lifecycle.
 */

interface PositionRecord {
  pos: PaperPosition;
  pricing: PoolPricingRef;
  fillCount: number;
  originalRawBaseAmount: bigint;
  rawBaseAmount: bigint;
  entryTx?: string | undefined;
  exitTx?: string | undefined;
  executionJson?: string | undefined;
  momentumWindowMs?: number | undefined;
  ladder?: ExitLadder;
  ladderTimer?: NodeJS.Timeout;
  exiting: boolean;
  /** Last observed price (for force-close when no fresh tick is available). */
  lastPrice: number;
  monitor: EmergencyMonitor;
  highVolatility: boolean;
  entrySoftScore: number | null;
  feedSource: string | null;
  venue: string | null;
  /** Max favorable excursion % from entry while open. */
  mfePct: number;
  /** Max adverse excursion % from entry while open (negative or zero). */
  maePct: number;
  timeToMfeMs: number | null;
  timeToMaeMs: number | null;
  pathMarks: Record<string, number>;
  features: StrategyFeatureFields;
  detectToOpenMs: number | null;
  relaxedRisk: boolean;
  relaxedReasons: string[];
}

const PATH_HORIZONS_MS = [1_000, 5_000, 15_000, 30_000, 60_000] as const;

export class PositionManager {
  private readonly config: Config;
  private readonly bus: TypedBus;
  private readonly repos: Repositories;
  private readonly poller: PricePoller;
  private readonly log = logger.child({ mod: 'positions' });
  private readonly positions = new Map<Mint, PositionRecord>();
  private readonly pendingEntries = new Set<Mint>();
  private readonly pendingRelaxedEntries = new Set<Mint>();
  private readonly now: () => number;
  /** dry-run/live: builds + broadcasts real buy/sell txs alongside the FSM. */
  private readonly executor: Executor | undefined;
  private readonly exitSupervisor: ExitSupervisor | undefined;
  private readonly risk: { canEnter(): { ok: boolean; reason?: string; detail?: string } } | undefined;
  private unsubscribe: (() => void) | null = null;
  private unsubscribeKill: (() => void) | null = null;

  constructor(deps: {
    config: Config;
    bus: TypedBus;
    repos: Repositories;
    poller: PricePoller;
    executor?: Executor;
    risk?: { canEnter(): { ok: boolean; reason?: string; detail?: string } };
    now?: () => number;
  }) {
    this.config = deps.config;
    this.bus = deps.bus;
    this.repos = deps.repos;
    this.poller = deps.poller;
    this.executor = deps.executor;
    this.now = deps.now ?? (() => Date.now());
    this.exitSupervisor = deps.executor
      ? new ExitSupervisor({ config: deps.config, bus: deps.bus, repos: deps.repos, executor: deps.executor, now: this.now })
      : undefined;
    this.risk = deps.risk;
  }

  start(): void {
    this.poller.setHandler((tick) => this.onTick(tick));
    this.poller.start();
    this.unsubscribe = this.bus.on('openPosition', (e) =>
      void this.open(
        e.mint,
        e.sizeSol,
        e.highVolatility,
        e.pricing,
        e.momentumWindowMs,
        e.relaxedRisk ?? false,
        e.relaxedReasons ?? [],
      ),
    );
    this.unsubscribeKill = this.bus.on('killSwitch', () => this.forceCloseAll('KILL_SWITCH', 'kill switch'));
    this.log.info('position manager started', { mode: this.config.mode });
  }

  stop(): void {
    this.unsubscribe?.();
    this.unsubscribeKill?.();
    this.unsubscribe = null;
    this.unsubscribeKill = null;
    for (const rec of this.positions.values()) {
      if (rec.ladderTimer) clearInterval(rec.ladderTimer);
    }
    this.poller.stop();
  }

  /** Flatten every open position at its last observed price (kill switch / shutdown). */
  forceCloseAll(trigger: ExitTrigger, detail?: string): void {
    const mints = [...this.positions.keys()];
    if (mints.length === 0) return;
    this.log.warn('force-closing all positions', { count: mints.length, trigger, detail });
    for (const mint of mints) {
      const rec = this.positions.get(mint);
      if (!rec) continue;
      if (rec.exiting) continue;
      if (this.config.mode === 'live' && this.executor) {
        const fill = rec.pos.previewForceClose(rec.lastPrice, trigger);
        if (fill) {
          void this.executeExit(rec, fill, true);
          this.bus.emit('exitTriggered', { mint, trigger, detail: detail ?? 'force close' });
        }
        continue;
      }
      const fill = rec.pos.forceClose(rec.lastPrice, this.now(), trigger);
      if (fill) {
        rec.fillCount++;
        if (this.executor) void this.executeExit(rec, fill);
        this.bus.emit('exitTriggered', { mint, trigger, detail: detail ?? 'force close' });
      }
      if (rec.pos.state === 'CLOSED' && this.config.mode !== 'live') this.finalize(mint, rec, rec.lastPrice);
    }
  }

  /** Fire an EMERGENCY_EXIT, auto-blacklist the mint + creator, and alert. */
  private handleEmergency(mint: Mint, rec: PositionRecord, price: number, kind: string, detail: string): void {
    this.log.error('EMERGENCY EXIT', { mint, kind, detail });
    if (this.config.mode === 'live' && this.executor) {
      const fill = rec.pos.previewForceClose(price, 'EMERGENCY_EXIT');
      if (fill) {
        void this.executeExit(rec, fill, true);
        this.bus.emit('exitTriggered', { mint, trigger: 'EMERGENCY_EXIT', detail });
      }
      this.blacklistEmergency(mint, rec, kind);
      this.bus.emit('alert', {
        level: 'error',
        message: `🚨 EMERGENCY EXIT ${short(mint)} — ${kind}: ${detail} (creator blacklisted)`,
        telegram: true,
      });
      return;
    }
    const fill = rec.pos.forceClose(price, this.now(), 'EMERGENCY_EXIT');
    if (fill) {
      rec.fillCount++;
      if (this.executor) void this.executeExit(rec, fill);
      this.bus.emit('exitTriggered', { mint, trigger: 'EMERGENCY_EXIT', detail });
    }
    // Auto-blacklist (Section 6.5): this creator/mint just rugged us.
    this.blacklistEmergency(mint, rec, kind);
    this.bus.emit('alert', {
      level: 'error',
      message: `🚨 EMERGENCY EXIT ${short(mint)} — ${kind}: ${detail} (creator blacklisted)`,
      telegram: true,
    });
    if (rec.pos.state === 'CLOSED' && this.config.mode !== 'live') this.finalize(mint, rec, price);
  }

  private blacklistEmergency(mint: Mint, rec: PositionRecord, kind: string): void {
    try {
      this.repos.blacklistMint(mint, kind);
      if (rec.pricing.creator) this.repos.blacklistCreator(rec.pricing.creator, kind);
    } catch (err) {
      this.log.error('auto-blacklist failed', { mint, err });
    }
  }

  get openCount(): number {
    return this.positions.size;
  }

  async recoverOpenPositions(): Promise<void> {
    if (this.config.mode !== 'live' || !this.executor) return;
    const rows = this.repos.latestOpenPositions();
    if (rows.length === 0) return;
    this.log.warn('recovering live open positions from DB', { count: rows.length });
    for (const row of rows) {
      try {
        if (!row.pricingJson || row.entryPrice === null || !row.openedAt) {
          throw new Error('missing pricing/entry metadata');
        }
        const pricing = JSON.parse(row.pricingJson) as PoolPricingRef;
        const rawBaseAmount = await this.executor.reconcileTokenBalance(pricing.baseMint, pricing.baseIsToken2022 ?? false);
        if (rawBaseAmount <= 0n) {
          const relaxedRisk = row.relaxedRisk === 1;
          const relaxedReasons = parseJsonArray(row.relaxedReasonsJson);
          this.persistPosition({
            mint: row.mint,
            state: 'CLOSED',
            sizeSol: row.sizeSol,
            entryPrice: row.entryPrice,
            openedAt: Date.parse(row.openedAt),
            closedAt: this.now(),
            pnlSol: 0,
            pnlPct: 0,
          }, {
            entryTx: row.entryTx ?? undefined,
            exitTx: row.exitTx ?? undefined,
            pricingJson: row.pricingJson,
            executionJson: safeJson({ event: 'recovery_zero_balance', previous: row.executionJson }),
            momentumWindowMs: row.momentumWindowMs ?? undefined,
            relaxedRisk,
            relaxedReasonsJson: relaxedReasons.length ? JSON.stringify(relaxedReasons) : null,
          });
          this.bus.emit('alert', { level: 'warn', message: `recovery closed ${short(row.mint)} — wallet has zero token balance`, telegram: true });
          continue;
        }
        const relaxedRisk = row.relaxedRisk === 1;
        const relaxedReasons = parseJsonArray(row.relaxedReasonsJson);
        const pos = new PaperPosition({
          mint: row.mint,
          sizeSol: row.sizeSol,
          entryPrice: row.entryPrice,
          openedAtMs: Date.parse(row.openedAt),
          highVolatility: false,
          cfg: this.exitCfgFor(relaxedRisk),
        });
        const ladder = this.executor.buildExitLadder(pricing.poolAddress, pricing.baseMint);
        await ladder.refresh(rawBaseAmount);
        const meta = this.entryMeta(row.mint, false);
        const analytics = this.loadAnalyticsForMint(row.mint, false, Date.parse(row.openedAt));
        const rec: PositionRecord = {
          pos,
          pricing,
          fillCount: 0,
          originalRawBaseAmount: row.rawBaseAmount ? BigInt(row.rawBaseAmount) : rawBaseAmount,
          rawBaseAmount,
          entryTx: row.entryTx ?? undefined,
          executionJson: safeJson({ event: 'recovered', previous: row.executionJson, rawBaseAmount: rawBaseAmount.toString() }),
          momentumWindowMs: row.momentumWindowMs ?? undefined,
          ladder,
          lastPrice: row.entryPrice,
          monitor: new EmergencyMonitor(this.monitorCfgFor(relaxedRisk)),
          exiting: false,
          relaxedRisk,
          relaxedReasons,
          ...meta,
          mfePct: 0,
          maePct: 0,
          timeToMfeMs: null,
          timeToMaeMs: null,
          pathMarks: {},
          features: analytics.features,
          detectToOpenMs: analytics.detectToOpenMs,
        };
        rec.ladderTimer = setInterval(() => {
          void rec.ladder?.refresh(rec.rawBaseAmount).catch((err) => this.log.warn('exit ladder refresh failed', { mint: row.mint, err }));
        }, this.config.exits.ladderRefreshMs);
        this.positions.set(row.mint, rec);
        this.registerPricing(row.mint, pricing);
        this.bus.emit('alert', { level: 'warn', message: `recovered live open position ${short(row.mint)} (${rawBaseAmount.toString()} raw)`, telegram: true });
      } catch (err) {
        this.log.error('live recovery failed — engaging kill switch', { mint: row.mint, err });
        this.bus.emit('killSwitch', { source: 'internal', detail: `recovery failed for ${row.mint}: ${(err as Error).message}` });
        return;
      }
    }
  }

  async recoverExitingPositions(): Promise<void> {
    if (this.config.mode !== 'live' || !this.executor || !this.exitSupervisor) return;
    const rows = this.repos.latestExitingPositions();
    if (rows.length === 0) return;
    this.log.error('recovering unfinished live exits from DB', { count: rows.length });

    for (const row of rows) {
      try {
        if (!row.pricingJson || !row.exitIntentJson || row.entryPrice === null || !row.openedAt) {
          throw new Error('missing exit recovery metadata');
        }
        const pricing = JSON.parse(row.pricingJson) as PoolPricingRef;
        const intent = parseExitIntent(row.exitIntentJson);
        const remaining = await this.executor.reconcileTokenBalance(pricing.baseMint, pricing.baseIsToken2022 ?? false);
        if (remaining <= 0n) {
          const relaxedRisk = row.relaxedRisk === 1;
          const relaxedReasons = parseJsonArray(row.relaxedReasonsJson);
          this.persistPosition({
            mint: row.mint,
            state: 'CLOSED',
            sizeSol: row.sizeSol,
            entryPrice: row.entryPrice,
            openedAt: Date.parse(row.openedAt),
            closedAt: this.now(),
            exitTrigger: intent.trigger,
            pnlSol: 0,
            pnlPct: 0,
          }, {
            entryTx: row.entryTx ?? undefined,
            exitTx: row.exitTx ?? undefined,
            rawBaseAmount: 0n,
            pricingJson: row.pricingJson,
            executionJson: safeJson({ event: 'recovered_exit_zero_balance', previous: row.executionJson, exitIntent: intent }),
            exitIntentJson: row.exitIntentJson,
            momentumWindowMs: row.momentumWindowMs ?? undefined,
            relaxedRisk,
            relaxedReasonsJson: relaxedReasons.length ? JSON.stringify(relaxedReasons) : null,
          });
          this.bus.emit('alert', { level: 'warn', message: `recovered completed exit ${short(row.mint)} — wallet balance is zero`, telegram: true });
          continue;
        }

        const relaxedRisk = row.relaxedRisk === 1;
        const relaxedReasons = parseJsonArray(row.relaxedReasonsJson);
        const pos = new PaperPosition({
          mint: row.mint,
          sizeSol: row.sizeSol,
          entryPrice: row.entryPrice,
          openedAtMs: Date.parse(row.openedAt),
          highVolatility: false,
          cfg: this.exitCfgFor(relaxedRisk),
        });
        const ladder = this.executor.buildExitLadder(pricing.poolAddress, pricing.baseMint);
        await ladder.refresh(remaining);
        const originalRawBaseAmount = BigInt(intent.originalRawAmount);
        const meta = this.entryMeta(row.mint, false);
        const analytics = this.loadAnalyticsForMint(row.mint, false, Date.parse(row.openedAt));
        const rec: PositionRecord = {
          pos,
          pricing,
          fillCount: 0,
          originalRawBaseAmount,
          rawBaseAmount: remaining,
          entryTx: row.entryTx ?? undefined,
          exitTx: row.exitTx ?? undefined,
          executionJson: safeJson({ event: 'recovering_exit', previous: row.executionJson, rawBaseAmount: remaining.toString() }),
          momentumWindowMs: row.momentumWindowMs ?? undefined,
          ladder,
          lastPrice: row.entryPrice,
          monitor: new EmergencyMonitor(this.monitorCfgFor(relaxedRisk)),
          exiting: true,
          relaxedRisk,
          relaxedReasons,
          ...meta,
          mfePct: 0,
          maePct: 0,
          timeToMfeMs: null,
          timeToMaeMs: null,
          pathMarks: {},
          features: analytics.features,
          detectToOpenMs: analytics.detectToOpenMs,
        };
        rec.ladderTimer = setInterval(() => {
          void rec.ladder?.refresh(rec.rawBaseAmount).catch((err) => this.log.warn('exit ladder refresh failed', { mint: row.mint, err }));
        }, this.config.exits.ladderRefreshMs);
        this.positions.set(row.mint, rec);
        this.registerPricing(row.mint, pricing);

        const fill = this.fillFromIntent(intent, row.entryPrice, row.sizeSol);
        const outcome = await this.exitSupervisor.recoverExit({
          position: this.positionForExit(rec, intent.trigger),
          pricing,
          fill,
          fullRemainder: intent.fullRemainder,
          rawBaseAmount: remaining,
          originalRawBaseAmount,
          ladder,
          entryTx: rec.entryTx,
          executionJson: rec.executionJson,
          momentumWindowMs: rec.momentumWindowMs,
        }, intent);
        this.handleLiveExitOutcome(rec, fill, outcome);
      } catch (err) {
        this.log.error('live exit recovery failed — engaging kill switch', { mint: row.mint, err });
        this.bus.emit('killSwitch', { source: 'internal', detail: `exit recovery failed for ${row.mint}: ${(err as Error).message}` });
        return;
      }
    }
  }

  private async open(
    mint: Mint,
    sizeSol: number,
    highVolatility: boolean,
    pricing: PoolPricingRef,
    momentumWindowMs?: number,
    relaxedRisk = false,
    relaxedReasons: string[] = [],
  ): Promise<void> {
    if (this.config.mode === 'live' && this.executor) {
      await this.openLive(mint, sizeSol, highVolatility, pricing, momentumWindowMs, relaxedRisk, relaxedReasons);
      return;
    }
    await this.openPaperLike(mint, sizeSol, highVolatility, pricing, momentumWindowMs, relaxedRisk, relaxedReasons);
  }

  private canStartEntry(mint: Mint, relaxedRisk: boolean): boolean {
    if (this.positions.has(mint) || this.pendingEntries.has(mint)) return false;
    if (this.positions.size + this.pendingEntries.size >= this.config.risk.maxConcurrentPositions) {
      this.bus.emit('entryVetoed', { mint, reason: 'CIRCUIT_BREAKER', detail: 'max concurrent positions' });
      this.log.info('entry blocked — max concurrent positions', { mint, cap: this.config.risk.maxConcurrentPositions });
      return false;
    }
    if (relaxedRisk) {
      const openRelaxed = [...this.positions.values()].filter((rec) => rec.relaxedRisk).length;
      const pendingRelaxed = this.pendingRelaxedEntries.size;
      if (openRelaxed + pendingRelaxed >= this.config.guardrails.relaxedRiskMaxOpenPositions) {
        this.bus.emit('entryVetoed', { mint, reason: 'CIRCUIT_BREAKER', detail: 'max relaxed-risk positions' });
        this.log.info('entry blocked — max relaxed-risk positions', {
          mint,
          cap: this.config.guardrails.relaxedRiskMaxOpenPositions,
        });
        return false;
      }
    }
    // Defense in depth: the risk manager also gates entry at H10, but re-check
    // here in case a breaker tripped between screening and the open.
    const decision = this.risk?.canEnter();
    if (decision && !decision.ok) {
      const reason = decision.reason === 'KILL_SWITCH' ? 'KILL_SWITCH' : 'CIRCUIT_BREAKER';
      this.bus.emit('entryVetoed', { mint, reason, detail: `${decision.reason}: ${decision.detail ?? ''}` });
      this.log.info('entry blocked — risk breaker', { mint, reason: decision.reason, detail: decision.detail });
      return false;
    }
    return true;
  }

  private async openPaperLike(
    mint: Mint,
    sizeSol: number,
    highVolatility: boolean,
    pricing: PoolPricingRef,
    momentumWindowMs?: number,
    relaxedRisk = false,
    relaxedReasons: string[] = [],
  ): Promise<void> {
    if (!this.canStartEntry(mint, relaxedRisk)) return;
    this.pendingEntries.add(mint);
    if (relaxedRisk) this.pendingRelaxedEntries.add(mint);

    try {
      const fresh = await this.poller.readOnce({
        baseVault: pricing.baseVault,
        quoteVault: pricing.quoteVault,
        baseDecimals: pricing.baseDecimals,
      });
      const pricingForPosition: PoolPricingRef = fresh
        ? { ...pricing, baseReserve: fresh.baseReserve, quoteReserveLamports: fresh.quoteReserveLamports }
        : pricing;
      const entryPrice = fresh?.price ?? computePrice(pricing.baseReserve, pricing.quoteReserveLamports, pricing.baseDecimals);
      if (entryPrice <= 0) {
        this.log.warn('cannot open — invalid entry price', { mint });
        return;
      }

      const openedAtMs = this.now();
      const pos = new PaperPosition({
        mint,
        sizeSol,
        entryPrice,
        openedAtMs,
        highVolatility,
        cfg: this.exitCfgFor(relaxedRisk),
      });
      const monitor = new EmergencyMonitor(this.monitorCfgFor(relaxedRisk));
      const rawEstimate = rawAmountFromSize(sizeSol, entryPrice, pricingForPosition.baseDecimals);
      const meta = this.entryMeta(mint, highVolatility);
      const analytics = this.loadAnalyticsForMint(mint, highVolatility, openedAtMs);
      this.positions.set(mint, {
        pos,
        pricing: pricingForPosition,
        fillCount: 0,
        originalRawBaseAmount: rawEstimate,
        rawBaseAmount: rawEstimate,
        lastPrice: entryPrice,
        monitor,
        exiting: false,
        relaxedRisk,
        relaxedReasons,
        momentumWindowMs: analytics.features.momentumWindowMs ?? momentumWindowMs,
        ...meta,
        mfePct: 0,
        maePct: 0,
        timeToMfeMs: null,
        timeToMaeMs: null,
        pathMarks: {},
        features: analytics.features,
        detectToOpenMs: analytics.detectToOpenMs,
      });

      // Monitor the creator's base-token ATA for dev-dump (batched into the poll).
      let creatorAta: string | undefined;
      if (this.config.exits.creatorDumpEnabled && pricingForPosition.creator) {
        try {
          creatorAta = deriveAta(pricingForPosition.creator, pricingForPosition.baseMint, pricingForPosition.baseIsToken2022 ?? false);
        } catch (err) {
          this.log.warn('creator ATA derivation failed — dev-dump monitor disabled for this position', { mint, err });
        }
      }
      this.poller.register({
        mint,
        baseVault: pricingForPosition.baseVault,
        quoteVault: pricingForPosition.quoteVault,
        baseDecimals: pricingForPosition.baseDecimals,
        ...(creatorAta ? { creatorAta } : {}),
      });

      this.persist(pos, 'OPEN', entryPrice, sizeSol, openedAtMs, null, null, null, {
        rawBaseAmount: rawEstimate,
        pricingJson: safeJson(pricingForPosition),
        momentumWindowMs: analytics.features.momentumWindowMs ?? momentumWindowMs,
        relaxedRisk,
        relaxedReasonsJson: relaxedReasons.length ? JSON.stringify(relaxedReasons) : null,
        ...this.analyticsTxns(meta, analytics),
      });
      this.bus.emit('positionUpdate', this.toPosition(pos, 'OPEN', entryPrice, sizeSol, openedAtMs));
      this.bus.emit('alert', {
        level: 'info',
        message: `📈 opened ${short(mint)} — ${sizeSol.toFixed(3)} SOL @ ${entryPrice.toPrecision(4)}${highVolatility ? ' (high-vol)' : ''}${relaxedRisk ? ' (relaxed-risk)' : ''}`,
        telegram: true,
      });
      this.log.info('paper position opened', { mint, sizeSol, entryPrice, momentumWindowMs, relaxedRisk, relaxedReasons, openCount: this.positions.size, freshRead: Boolean(fresh) });

      // dry-run/live: build + broadcast the real buy (transcript in dry-run, send
      // in live). Fire-and-log; paper accounting drives the FSM either way.
      if (this.executor) void this.executeEntry(mint, pricingForPosition, sizeSol);
    } finally {
      this.pendingEntries.delete(mint);
      this.pendingRelaxedEntries.delete(mint);
    }
  }

  private async openLive(
    mint: Mint,
    sizeSol: number,
    highVolatility: boolean,
    pricing: PoolPricingRef,
    momentumWindowMs?: number,
    relaxedRisk = false,
    relaxedReasons: string[] = [],
  ): Promise<void> {
    if (!this.canStartEntry(mint, relaxedRisk)) return;
    const estimatedEntryPrice = computePrice(pricing.baseReserve, pricing.quoteReserveLamports, pricing.baseDecimals);
    if (estimatedEntryPrice <= 0) {
      this.log.warn('cannot open live — invalid entry price', { mint });
      return;
    }

    this.pendingEntries.add(mint);
    if (relaxedRisk) this.pendingRelaxedEntries.add(mint);
    const openedAtMs = this.now();
    const pending: Position = { mint, state: 'PENDING_ENTRY', sizeSol, entryPrice: estimatedEntryPrice, openedAt: openedAtMs };
    this.persistPosition(pending, {
      pricingJson: safeJson(pricing),
      executionJson: safeJson({ event: 'pending_entry' }),
      momentumWindowMs,
      relaxedRisk,
      relaxedReasonsJson: relaxedReasons.length ? JSON.stringify(relaxedReasons) : null,
    });
    this.bus.emit('positionUpdate', pending);

    try {
      const buy = await this.executor!.buyAndConfirm(pricing.poolAddress, pricing.baseMint, sizeSol);
      if (!buy.confirmed || !buy.signature) {
        this.failLiveEntry(mint, pending, buy, 'buy not confirmed', momentumWindowMs, relaxedRisk, relaxedReasons);
        return;
      }
      const rawBaseAmount = await this.executor!.reconcileTokenBalance(pricing.baseMint, pricing.baseIsToken2022 ?? false);
      if (rawBaseAmount <= 0n) {
        this.failLiveEntry(mint, pending, buy, 'confirmed buy but wallet has no base tokens', momentumWindowMs, relaxedRisk, relaxedReasons);
        return;
      }
      const entryPrice = entryPriceFromRawAmount(sizeSol, rawBaseAmount, pricing.baseDecimals) ?? estimatedEntryPrice;

      const pos = new PaperPosition({ mint, sizeSol, entryPrice, openedAtMs, highVolatility, cfg: this.exitCfgFor(relaxedRisk) });
      const monitor = new EmergencyMonitor(this.monitorCfgFor(relaxedRisk));
      const ladder = this.executor!.buildExitLadder(pricing.poolAddress, pricing.baseMint);
      await ladder.refresh(rawBaseAmount);
      const meta = this.entryMeta(mint, highVolatility);
      const analytics = this.loadAnalyticsForMint(mint, highVolatility, openedAtMs);
      const rec: PositionRecord = {
        pos,
        pricing,
        fillCount: 0,
        originalRawBaseAmount: rawBaseAmount,
        rawBaseAmount,
        entryTx: buy.signature,
        executionJson: safeJson({ entry: buy, rawBaseAmount: rawBaseAmount.toString() }),
        momentumWindowMs: analytics.features.momentumWindowMs ?? momentumWindowMs,
        ladder,
        lastPrice: entryPrice,
        monitor,
        exiting: false,
        relaxedRisk,
        relaxedReasons,
        ...meta,
        mfePct: 0,
        maePct: 0,
        timeToMfeMs: null,
        timeToMaeMs: null,
        pathMarks: {},
        features: analytics.features,
        detectToOpenMs: analytics.detectToOpenMs,
      };
      rec.ladderTimer = setInterval(() => {
        void rec.ladder?.refresh(rec.rawBaseAmount).catch((err) => {
          this.log.warn('exit ladder refresh failed', { mint, err });
        });
      }, this.config.exits.ladderRefreshMs);
      this.positions.set(mint, rec);
      this.registerPricing(mint, pricing);
      this.persistPosition({ mint, state: 'OPEN', sizeSol, entryPrice, openedAt: openedAtMs }, {
        entryTx: buy.signature,
        ...this.analyticsTxns(meta, analytics),
        rawBaseAmount,
        pricingJson: safeJson(pricing),
        executionJson: rec.executionJson,
        momentumWindowMs: analytics.features.momentumWindowMs ?? momentumWindowMs,
        relaxedRisk,
        relaxedReasonsJson: relaxedReasons.length ? JSON.stringify(relaxedReasons) : null,
      });
      this.bus.emit('positionUpdate', { mint, state: 'OPEN', sizeSol, entryPrice, openedAt: openedAtMs });
      this.bus.emit('alert', {
        level: 'info',
        message: `📈 live opened ${short(mint)} — ${sizeSol.toFixed(3)} SOL @ ${entryPrice.toPrecision(4)} (${rawBaseAmount.toString()} raw)${relaxedRisk ? ' (relaxed-risk)' : ''}`,
        telegram: true,
      });
      this.log.info('live position opened', { mint, sizeSol, entryPrice, relaxedRisk, relaxedReasons, rawBaseAmount: rawBaseAmount.toString(), tx: buy.signature });
    } catch (err) {
      this.persistPosition({ ...pending, state: 'FAILED' }, {
        pricingJson: safeJson(pricing),
        executionJson: safeJson({ event: 'entry_exception', error: (err as Error).message }),
        momentumWindowMs,
        relaxedRisk,
        relaxedReasonsJson: relaxedReasons.length ? JSON.stringify(relaxedReasons) : null,
      });
      this.bus.emit('alert', { level: 'error', message: `live entry failed ${short(mint)} — ${(err as Error).message}`, telegram: true });
      this.log.error('live entry failed', { mint, err });
    } finally {
      this.pendingEntries.delete(mint);
      this.pendingRelaxedEntries.delete(mint);
    }
  }

  private async executeEntry(mint: Mint, pricing: PoolPricingRef, sizeSol: number): Promise<void> {
    try {
      await this.executor!.buy(pricing.poolAddress, pricing.baseMint, sizeSol);
    } catch (err) {
      this.log.error('entry execution failed', { mint, err });
    }
  }

  private failLiveEntry(
    mint: Mint,
    pending: Position,
    result: BroadcastResult,
    detail: string,
    momentumWindowMs?: number,
    relaxedRisk = false,
    relaxedReasons: string[] = [],
  ): void {
    this.persistPosition({ ...pending, state: 'FAILED' }, {
      pricingJson: safeJson({ mint }),
      executionJson: safeJson({ event: 'entry_failed', detail, result }),
      momentumWindowMs,
      relaxedRisk,
      relaxedReasonsJson: relaxedReasons.length ? JSON.stringify(relaxedReasons) : null,
    });
    this.bus.emit('positionUpdate', { ...pending, state: 'FAILED' });
    this.bus.emit('alert', { level: 'error', message: `live entry failed ${short(mint)} — ${detail}`, telegram: true });
    this.log.error('live entry failed', { mint, detail, result });
  }

  private registerPricing(mint: Mint, pricing: PoolPricingRef): void {
    let creatorAta: string | undefined;
    if (this.config.exits.creatorDumpEnabled && pricing.creator) {
      try {
        creatorAta = deriveAta(pricing.creator, pricing.baseMint, pricing.baseIsToken2022 ?? false);
      } catch (err) {
        this.log.warn('creator ATA derivation failed — dev-dump monitor disabled for this position', { mint, err });
      }
    }
    this.poller.register({
      mint,
      baseVault: pricing.baseVault,
      quoteVault: pricing.quoteVault,
      baseDecimals: pricing.baseDecimals,
      ...(creatorAta ? { creatorAta } : {}),
    });
  }

  private exitCfgFor(relaxedRisk: boolean): Config['exits'] {
    if (!relaxedRisk) return this.config.exits;
    return {
      ...this.config.exits,
      tp0Enabled: this.config.guardrails.relaxedRiskTp0Enabled || this.config.exits.tp0Enabled,
      timeStopMinutes: Math.min(this.config.exits.timeStopMinutes, this.config.guardrails.relaxedRiskTimeStopMinutes),
      trailingGapPct: Math.min(this.config.exits.trailingGapPct, this.config.guardrails.relaxedRiskTrailingGapPct),
    };
  }

  private monitorCfgFor(relaxedRisk: boolean): EmergencyMonitorConfig {
    return {
      lpDropPct: relaxedRisk
        ? Math.min(this.config.exits.emergencyLpDropPct, this.config.guardrails.relaxedRiskEmergencyLpDropPct)
        : this.config.exits.emergencyLpDropPct,
      windowTicks: this.config.exits.lpDropWindowTicks,
      creatorDumpEnabled: this.config.exits.creatorDumpEnabled,
      creatorDumpPct: this.config.exits.creatorDumpThresholdPct,
    };
  }

  private async executeExit(rec: PositionRecord, fill: Fill, forceFullRemainder = false): Promise<void> {
    if (this.config.mode === 'live' && this.executor) {
      await this.executeLiveExit(rec, fill, forceFullRemainder);
      return;
    }
    // Raw base-token units for this fill's fraction of the original position.
    const wholeTokens = rec.pos.sizeSol / rec.pos.entryPrice;
    const rawBase = BigInt(Math.floor(wholeTokens * fill.fraction * 10 ** rec.pricing.baseDecimals));
    if (rawBase <= 0n) return;
    try {
      await this.executor!.sell(rec.pricing.poolAddress, rec.pricing.baseMint, rawBase, this.config.entry.maxSlippagePct);
    } catch (err) {
      this.log.error('exit execution failed', { mint: rec.pos.mint, err });
    }
  }

  private async executeLiveExit(rec: PositionRecord, fill: Fill, forceFullRemainder = false): Promise<BroadcastResult | null> {
    if (!this.executor || !this.exitSupervisor || rec.exiting) return null;
    rec.exiting = true;
    const fullRemainder = forceFullRemainder || fill.fraction >= rec.pos.remainingFraction - 1e-9;
    try {
      const outcome = await this.exitSupervisor.startExit({
        position: this.positionForExit(rec, fill.trigger),
        pricing: rec.pricing,
        fill,
        fullRemainder,
        rawBaseAmount: rec.rawBaseAmount,
        originalRawBaseAmount: rec.originalRawBaseAmount,
        ladder: rec.ladder,
        entryTx: rec.entryTx,
        executionJson: rec.executionJson,
        momentumWindowMs: rec.momentumWindowMs,
      });
      this.handleLiveExitOutcome(rec, fill, outcome);
      return outcome.result ?? null;
    } catch (err) {
      this.log.error('live exit execution failed', { mint: rec.pos.mint, err });
      this.bus.emit('alert', { level: 'error', message: `live exit failed ${short(rec.pos.mint)} — ${(err as Error).message}`, telegram: true });
      return null;
    }
  }

  private onTick(tick: PriceTick): void {
    const rec = this.positions.get(tick.mint);
    if (!rec) return;
    if (rec.exiting) return;

    // Persist the tick for replay/tuning (hourly prune handles retention).
    try {
      this.repos.insertPriceTick({
        mint: tick.mint,
        slot: null,
        price: tick.price,
        solReserve: Number(tick.quoteReserveLamports) / LAMPORTS_PER_SOL,
      });
    } catch (err) {
      this.log.debug('price tick persist failed', { mint: tick.mint, err });
    }

    rec.lastPrice = tick.price;
    this.updateExcursions(rec, tick.price);

    // In-position emergency check (LP pull / creator dump) — worst-case exit.
    const signal = rec.monitor.onTick({
      quoteReserveLamports: tick.quoteReserveLamports,
      ...(tick.creatorBaseBalance !== undefined ? { creatorBaseBalance: tick.creatorBaseBalance } : {}),
    });
    if (signal && rec.pos.state === 'OPEN') {
      this.handleEmergency(tick.mint, rec, tick.price, signal.kind, signal.detail);
      return;
    }

    if (this.config.mode === 'live' && this.executor) {
      const fill = rec.pos.previewPriceExit(tick.price, tick.atMs);
      if (!fill) return;
      const fullRemainder = fill.fraction >= rec.pos.remainingFraction - 1e-9;
      void this.executeExit(rec, fill, fullRemainder);
      this.bus.emit('exitTriggered', { mint: tick.mint, trigger: fill.trigger, detail: fill.reason });
      this.bus.emit('alert', {
        level: 'info',
        message:
          `↗ exit ${short(tick.mint)} — ${fill.trigger} ${Math.round(fill.fraction * 100)}% ` +
          `· pnl ${fill.pnlSol >= 0 ? '+' : ''}${fill.pnlSol.toFixed(4)} SOL`,
        telegram: true,
      });
      this.log.info('live exit intent created', {
        mint: tick.mint,
        trigger: fill.trigger,
        fraction: Number(fill.fraction.toFixed(3)),
        gainPct: Number(fill.gainPct.toFixed(1)),
      });
      return;
    }

    const fills = rec.pos.onPrice(tick.price, tick.atMs);
    for (const fill of fills) {
      rec.fillCount++;
      this.recordFill(rec, fill, tick.atMs);
      if (this.executor) void this.executeExit(rec, fill);
      this.bus.emit('exitTriggered', { mint: tick.mint, trigger: fill.trigger, detail: fill.reason });
      this.bus.emit('alert', {
        level: 'info',
        message:
          `↗ exit ${short(tick.mint)} — ${fill.trigger} ${Math.round(fill.fraction * 100)}% ` +
          `· pnl ${fill.pnlSol >= 0 ? '+' : ''}${fill.pnlSol.toFixed(4)} SOL`,
        telegram: true,
      });
      this.log.info('exit fill', {
        mint: tick.mint,
        trigger: fill.trigger,
        fraction: Number(fill.fraction.toFixed(3)),
        gainPct: Number(fill.gainPct.toFixed(1)),
        pnlSol: Number(fill.pnlSol.toFixed(5)),
      });
    }

    if (rec.pos.state === 'CLOSED' && this.config.mode !== 'live') this.finalize(tick.mint, rec, tick.price);
  }

  private finalize(
    mint: Mint,
    rec: PositionRecord,
    exitPrice: number,
    txns: { exitTx?: string | undefined; executionJson?: string | undefined; exitTriggerToConfirmMs?: number | undefined } = {},
  ): void {
    const gross = rec.pos.realizedPnlSol;
    const fees = this.estimateFees(rec.pos.sizeSol, rec.fillCount);
    const net = gross - fees;
    const pnlPct = (net / rec.pos.sizeSol) * 100;
    const closedAt = rec.pos.closedAtMs ?? this.now();
    const holdMs = closedAt - rec.pos.openedAtMs;
    this.updateExcursions(rec, exitPrice);

    this.poller.unregister(mint);
    if (rec.ladderTimer) clearInterval(rec.ladderTimer);
    this.positions.delete(mint);

    const position: Position = {
      mint,
      state: 'CLOSED',
      sizeSol: rec.pos.sizeSol,
      entryPrice: rec.pos.entryPrice,
      openedAt: rec.pos.openedAtMs,
      closedAt,
      ...(rec.pos.lastTrigger ? { exitTrigger: rec.pos.lastTrigger } : {}),
      pnlSol: net,
      pnlPct,
    };
    const leftOnTablePct = rec.mfePct - pnlPct;
    this.persist(rec.pos, 'CLOSED', rec.pos.entryPrice, rec.pos.sizeSol, rec.pos.openedAtMs, closedAt, net, pnlPct, {
      entryTx: rec.entryTx,
      exitTx: txns.exitTx ?? rec.exitTx,
      exitPrice,
      rawBaseAmount: rec.rawBaseAmount,
      pricingJson: safeJson(rec.pricing),
      executionJson: txns.executionJson ?? rec.executionJson,
      exitTriggerToConfirmMs: txns.exitTriggerToConfirmMs,
      momentumWindowMs: rec.momentumWindowMs,
      relaxedRisk: rec.relaxedRisk,
      relaxedReasonsJson: rec.relaxedReasons.length ? JSON.stringify(rec.relaxedReasons) : null,
      grossPnlSol: gross,
      feesSol: fees,
      netPnlSol: net,
      entrySoftScore: rec.entrySoftScore ?? undefined,
      highVolatility: rec.highVolatility,
      mfePct: rec.mfePct,
      maePct: rec.maePct,
      holdMs,
      feedSource: rec.feedSource ?? undefined,
      venue: rec.venue ?? undefined,
      mode: this.config.mode,
      timeToMfeMs: rec.timeToMfeMs ?? undefined,
      timeToMaeMs: rec.timeToMaeMs ?? undefined,
      pathMarksJson: Object.keys(rec.pathMarks).length ? JSON.stringify(rec.pathMarks) : undefined,
      leftOnTablePct,
      detectToOpenMs: rec.detectToOpenMs ?? undefined,
      ...featureFieldsFrom(rec.features),
    });
    if (txns.exitTriggerToConfirmMs !== undefined && Number.isFinite(txns.exitTriggerToConfirmMs)) {
      try {
        this.repos.recordLatencySample({
          kind: 'exit_confirm',
          latencyMs: txns.exitTriggerToConfirmMs,
          mint,
          ...(rec.feedSource ? { feedSource: rec.feedSource } : {}),
        });
      } catch (err) {
        this.log.debug('exit latency sample failed', { mint, err });
      }
    }
    this.bus.emit('positionUpdate', position);

    const emoji = net >= 0 ? '✅' : '🔻';
    this.bus.emit('alert', {
      level: 'info',
      message: `${emoji} closed ${short(mint)} — ${rec.pos.lastTrigger} · net ${net >= 0 ? '+' : ''}${net.toFixed(4)} SOL (${pnlPct >= 0 ? '+' : ''}${pnlPct.toFixed(1)}%, gross ${gross.toFixed(4)}, fees ${fees.toFixed(4)})`,
      telegram: true,
    });
    this.log.info('paper position closed', {
      mint,
      trigger: rec.pos.lastTrigger,
      grossPnlSol: Number(gross.toFixed(5)),
      feesSol: Number(fees.toFixed(5)),
      netPnlSol: Number(net.toFixed(5)),
      pnlPct: Number(pnlPct.toFixed(1)),
      exitPrice,
    });
  }

  private handleLiveExitOutcome(rec: PositionRecord, fill: Fill, outcome: ExitOutcome): void {
    rec.rawBaseAmount = outcome.remainingRawAmount;
    rec.exitTx = outcome.exitTx ?? outcome.result?.signature ?? rec.exitTx;
    rec.executionJson = safeJson({
      ...(parseJsonObject(rec.executionJson)),
      lastExit: outcome.result,
      exitIntent: outcome.intent,
      remainingRawAmount: outcome.remainingRawAmount.toString(),
    });

    if (!outcome.confirmed) {
      rec.exiting = true;
      return;
    }

    rec.pos.applyFill(fill, this.now());
    rec.fillCount++;
    this.recordFill(rec, fill, this.now());
    rec.exiting = false;

    if (rec.rawBaseAmount > 0n && rec.ladder) {
      void rec.ladder.refresh(rec.rawBaseAmount).catch((err) => this.log.warn('exit ladder refresh failed', { mint: rec.pos.mint, err }));
    }

    if (rec.rawBaseAmount <= 0n || rec.pos.state === 'CLOSED') {
      this.finalize(rec.pos.mint, rec, fill.price, {
        exitTx: rec.exitTx,
        executionJson: rec.executionJson,
        exitTriggerToConfirmMs: outcome.exitTriggerToConfirmMs,
      });
      return;
    }

    this.persistPosition({
      mint: rec.pos.mint,
      state: 'OPEN',
      sizeSol: rec.pos.sizeSol,
      entryPrice: rec.pos.entryPrice,
      openedAt: rec.pos.openedAtMs,
    }, {
      entryTx: rec.entryTx,
      exitTx: rec.exitTx,
      rawBaseAmount: rec.rawBaseAmount,
      pricingJson: safeJson(rec.pricing),
      executionJson: rec.executionJson,
      exitTriggerToConfirmMs: outcome.exitTriggerToConfirmMs,
      momentumWindowMs: rec.momentumWindowMs,
      relaxedRisk: rec.relaxedRisk,
      relaxedReasonsJson: rec.relaxedReasons.length ? JSON.stringify(rec.relaxedReasons) : null,
    });
  }

  private positionForExit(rec: PositionRecord, trigger: ExitTrigger): Position {
    return {
      mint: rec.pos.mint,
      state: 'EXITING',
      sizeSol: rec.pos.sizeSol,
      entryPrice: rec.pos.entryPrice,
      openedAt: rec.pos.openedAtMs,
      exitTrigger: trigger,
    };
  }

  private fillFromIntent(intent: { trigger: ExitTrigger; fullRemainder: boolean; targetRawAmount: string; originalRawAmount: string }, price: number, sizeSol: number): Fill {
    let fraction = 1;
    if (!intent.fullRemainder) {
      const original = BigInt(intent.originalRawAmount);
      fraction = original > 0n ? Number((BigInt(intent.targetRawAmount) * 1_000_000n) / original) / 1_000_000 : 0;
    }
    return {
      trigger: intent.trigger,
      fraction,
      price,
      gainPct: 0,
      pnlSol: 0,
      reason: 'recovered exit intent',
    };
  }

  /** Paper fee drag: priority+tip per tx (entry + each exit) and swap fee per leg. */
  private estimateFees(sizeSol: number, exitFills: number): number {
    const txCount = 1 + Math.max(1, exitFills);
    const tip = this.config.fees.estPriorityTipSolPerTx * txCount;
    const swap = (this.config.fees.swapFeePct / 100) * sizeSol * 2; // entry + exit legs
    return tip + swap;
  }

  private toPosition(
    pos: PaperPosition,
    state: Position['state'],
    entryPrice: number,
    sizeSol: number,
    openedAt: number,
  ): Position {
    return { mint: pos.mint, state, sizeSol, entryPrice, openedAt };
  }

  private persist(
    pos: PaperPosition,
    state: Position['state'],
    entryPrice: number,
    sizeSol: number,
    openedAt: number,
    closedAt: number | null,
    pnlSol: number | null,
    pnlPct: number | null,
    txns: Parameters<Repositories['upsertPosition']>[1] = {},
  ): void {
    try {
      const p: Position = {
        mint: pos.mint,
        state,
        sizeSol,
        entryPrice,
        openedAt,
        ...(closedAt ? { closedAt } : {}),
        ...(pos.lastTrigger ? { exitTrigger: pos.lastTrigger } : {}),
        ...(pnlSol !== null ? { pnlSol } : {}),
        ...(pnlPct !== null ? { pnlPct } : {}),
      };
      this.repos.upsertPosition(p, txns);
    } catch (err) {
      this.log.error('failed to persist position', { mint: pos.mint, state, err });
    }
  }

  private persistPosition(
    p: Position,
    txns: Parameters<Repositories['upsertPosition']>[1] = {},
  ): void {
    try {
      this.repos.upsertPosition(p, txns);
    } catch (err) {
      this.log.error('failed to persist position', { mint: p.mint, state: p.state, err });
    }
  }

  private entryMeta(mint: Mint, highVolatility: boolean): {
    highVolatility: boolean;
    entrySoftScore: number | null;
    feedSource: string | null;
    venue: string | null;
  } {
    let entrySoftScore: number | null = null;
    let feedSource: string | null = null;
    let venue: string | null = null;
    try {
      entrySoftScore = this.repos.latestSoftScore(mint);
      const g = this.repos.latestGraduationMeta(mint);
      feedSource = g.feedSource;
      venue = g.venue;
    } catch (err) {
      this.log.debug('entry meta lookup failed', { mint, err });
    }
    return { highVolatility, entrySoftScore, feedSource, venue };
  }

  private analyticsTxns(
    meta: {
      highVolatility: boolean;
      entrySoftScore: number | null;
      feedSource: string | null;
      venue: string | null;
    },
    analytics: { features: StrategyFeatureFields; detectToOpenMs: number | null },
  ): Parameters<Repositories['upsertPosition']>[1] {
    return {
      ...(meta.entrySoftScore !== null ? { entrySoftScore: meta.entrySoftScore } : {}),
      highVolatility: meta.highVolatility,
      ...(meta.feedSource ? { feedSource: meta.feedSource } : {}),
      ...(meta.venue ? { venue: meta.venue } : {}),
      mode: this.config.mode,
      ...(analytics.detectToOpenMs !== null ? { detectToOpenMs: analytics.detectToOpenMs } : {}),
      ...featureFieldsFrom(analytics.features),
    };
  }

  private loadAnalyticsForMint(
    mint: Mint,
    highVolatility: boolean,
    openedAtMs: number,
  ): { features: StrategyFeatureFields; detectToOpenMs: number | null } {
    const session = getActiveRunSession();
    let features: StrategyFeatureFields = {
      sessionId: session?.id ?? null,
      configHash: session?.configHash ?? null,
    };
    let detectToOpenMs: number | null = null;
    try {
      const cand = this.repos.latestCandidateFeatures(mint);
      features = {
        sessionId: cand.sessionId ?? session?.id ?? null,
        configHash: cand.configHash ?? session?.configHash ?? null,
        sizeMultiplier: cand.sizeMultiplier,
        earlyFlowNetSol: cand.earlyFlowNetSol,
        earlyFlowRate: cand.earlyFlowRate,
        poolSolAtEntry: cand.poolSolAtEntry,
        buyImpactPct: cand.buyImpactPct,
        top10Share: cand.top10Share,
        maxHolderShare: cand.maxHolderShare,
        creatorShare: cand.creatorShare,
        rugcheckScore: cand.rugcheckScore,
        hasSocials: cand.hasSocials,
        scoreComponentsJson: cand.scoreComponentsJson,
        unknownsJson: cand.unknownsJson,
        enrichmentMs: cand.enrichmentMs,
        momentumWindowMs: cand.momentumWindowMs,
        relaxedRisk: cand.relaxedRisk,
        relaxedReasonsJson: cand.relaxedReasonsJson,
        sellabilityReason: cand.sellabilityReason,
      };
      void highVolatility;
      const gradMs = this.repos.graduationCreatedAtMs(mint);
      if (gradMs !== null) detectToOpenMs = Math.max(0, openedAtMs - gradMs);
    } catch (err) {
      this.log.debug('load analytics for mint failed', { mint, err });
    }
    return { features, detectToOpenMs };
  }

  private recordFill(rec: PositionRecord, fill: Fill, atMs: number): void {
    try {
      this.repos.recordPositionFill({
        mint: rec.pos.mint,
        sessionId: rec.features.sessionId ?? null,
        trigger: fill.trigger,
        fraction: fill.fraction,
        price: fill.price,
        gainPct: fill.gainPct,
        pnlSol: fill.pnlSol,
        remainingFraction: rec.pos.remainingFraction,
        atMs,
      });
    } catch (err) {
      this.log.debug('fill persist failed', { mint: rec.pos.mint, err });
    }
  }

  private updateExcursions(rec: PositionRecord, price: number): void {
    const entry = rec.pos.entryPrice;
    if (!(entry > 0) || !(price > 0)) return;
    const pct = ((price - entry) / entry) * 100;
    const ageMs = this.now() - rec.pos.openedAtMs;
    if (pct > rec.mfePct) {
      rec.mfePct = pct;
      rec.timeToMfeMs = ageMs;
    }
    if (pct < rec.maePct) {
      rec.maePct = pct;
      rec.timeToMaeMs = ageMs;
    }
    for (const h of PATH_HORIZONS_MS) {
      const key = `+${h}ms`;
      if (ageMs >= h && rec.pathMarks[key] === undefined) {
        rec.pathMarks[key] = pct;
      }
    }
  }
}

function featureFieldsFrom(f: StrategyFeatureFields): StrategyFeatureFields {
  return {
    ...(f.sessionId !== undefined && f.sessionId !== null ? { sessionId: f.sessionId } : {}),
    ...(f.configHash ? { configHash: f.configHash } : {}),
    ...(f.sizeMultiplier !== undefined && f.sizeMultiplier !== null ? { sizeMultiplier: f.sizeMultiplier } : {}),
    ...(f.earlyFlowNetSol !== undefined && f.earlyFlowNetSol !== null ? { earlyFlowNetSol: f.earlyFlowNetSol } : {}),
    ...(f.earlyFlowRate !== undefined && f.earlyFlowRate !== null ? { earlyFlowRate: f.earlyFlowRate } : {}),
    ...(f.poolSolAtEntry !== undefined && f.poolSolAtEntry !== null ? { poolSolAtEntry: f.poolSolAtEntry } : {}),
    ...(f.buyImpactPct !== undefined && f.buyImpactPct !== null ? { buyImpactPct: f.buyImpactPct } : {}),
    ...(f.top10Share !== undefined && f.top10Share !== null ? { top10Share: f.top10Share } : {}),
    ...(f.maxHolderShare !== undefined && f.maxHolderShare !== null ? { maxHolderShare: f.maxHolderShare } : {}),
    ...(f.creatorShare !== undefined && f.creatorShare !== null ? { creatorShare: f.creatorShare } : {}),
    ...(f.rugcheckScore !== undefined && f.rugcheckScore !== null ? { rugcheckScore: f.rugcheckScore } : {}),
    ...(f.hasSocials !== undefined && f.hasSocials !== null ? { hasSocials: f.hasSocials } : {}),
    ...(f.scoreComponentsJson ? { scoreComponentsJson: f.scoreComponentsJson } : {}),
    ...(f.unknownsJson ? { unknownsJson: f.unknownsJson } : {}),
    ...(f.enrichmentMs !== undefined && f.enrichmentMs !== null ? { enrichmentMs: f.enrichmentMs } : {}),
    ...(f.momentumWindowMs !== undefined && f.momentumWindowMs !== null ? { momentumWindowMs: f.momentumWindowMs } : {}),
    ...(f.relaxedRisk !== undefined && f.relaxedRisk !== null ? { relaxedRisk: f.relaxedRisk } : {}),
    ...(f.relaxedReasonsJson ? { relaxedReasonsJson: f.relaxedReasonsJson } : {}),
    ...(f.sellabilityReason ? { sellabilityReason: f.sellabilityReason } : {}),
  };
}

function short(mint: string): string {
  return mint.length > 10 ? `${mint.slice(0, 4)}…${mint.slice(-4)}` : mint;
}

function rawAmountFromSize(sizeSol: number, price: number, decimals: number): bigint {
  if (price <= 0) return 0n;
  return BigInt(Math.floor((sizeSol / price) * 10 ** decimals));
}

function entryPriceFromRawAmount(sizeSol: number, rawBaseAmount: bigint, decimals: number): number | null {
  if (rawBaseAmount <= 0n) return null;
  const tokens = Number(rawBaseAmount) / 10 ** decimals;
  return tokens > 0 ? sizeSol / tokens : null;
}

function safeJson(value: unknown): string {
  return JSON.stringify(value, (_k, v) => (typeof v === 'bigint' ? v.toString() : v));
}

function parseJsonObject(value: string | undefined): Record<string, unknown> {
  if (!value) return {};
  try {
    const parsed = JSON.parse(value) as unknown;
    return parsed && typeof parsed === 'object' && !Array.isArray(parsed) ? parsed as Record<string, unknown> : {};
  } catch {
    return {};
  }
}

function parseJsonArray(value: string | null | undefined): string[] {
  if (!value) return [];
  try {
    const parsed = JSON.parse(value) as unknown;
    return Array.isArray(parsed) ? parsed.filter((v): v is string => typeof v === 'string') : [];
  } catch {
    return [];
  }
}

function minBigint(a: bigint, b: bigint): bigint {
  return a < b ? a : b;
}
