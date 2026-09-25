import type { Config } from '../config/schema.ts';
import type { TypedBus } from '../core/bus.ts';
import type { Repositories } from '../persistence/repositories.ts';
import type { Mint, Position, PoolPricingRef, ExitTrigger } from '../core/types.ts';
import { LAMPORTS_PER_SOL } from '../core/constants.ts';
import { logger } from '../core/logger.ts';
import { PaperPosition, type Fill } from './position.ts';
import { baseReserveWhole, buyImpactSol, estimatePaperFees, sellImpactSol } from './paperFees.ts';
import { computePrice, type PoolRef, type PriceIngest, type PricePoller, type PriceTick } from './pricing.ts';
import { EmergencyMonitor, creatorAtaFor, monitorCfgFor, type EmergencyMonitorConfig } from './monitors.ts';
import type { Executor } from '../executor/index.ts';
import type { BroadcastResult } from '../executor/broadcaster.ts';
import { EntryMoveExceeded } from '../executor/slippage.ts';
import type { ExitLadder } from './presign.ts';
import { ExitSupervisor, parseExitIntent, type ExitOutcome } from './exitSupervisor.ts';
import { exitCfgFor } from '../exits/engine.ts';
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
  /** Reserves on the most recent tick (paper sell-impact on force-closes). */
  lastBaseReserve: bigint;
  /** Modelled constant-product impact, paper/dry-run only (live fills embed it). */
  slippageSol: number;
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
  /**
   * Tick accounting. The exit FSM is driven purely by ticks, and it was running
   * blind: 14 of 25 live positions exited on a SINGLE price observation (all 14
   * lost, median -22.6%, and live never once reached TAKE_PROFIT_1). These are
   * counted here rather than derived from `price_ticks` so pre-entry
   * registration cannot corrupt the count.
   */
  tickCount: number;
  /** Ticks rejected as non-finite / <= 0 — never reached lastPrice or the FSM. */
  suspectTickCount: number;
  firstTickAtMs: number | null;
  lastTickAtMs: number | null;
  /** Set once the blind guard has issued its force-read, so it fires only once. */
  forcedReadAtMs: number | null;
  /**
   * Mid move from the screening snapshot to the ACTUAL fill, in percent.
   * `execution_json.entry.entryMovePct` only spans verdict -> buy quote (~0.1 s)
   * and had no predictive power (recorded range +6.78% max, down to -77%, all 14
   * of them stop-losses regardless of sign). This spans the whole entry.
   */
  entryMoveFromDetectPct: number | null;
}

const PATH_HORIZONS_MS = [1_000, 5_000, 15_000, 30_000, 60_000] as const;

/** Rate limit on the degraded-poller alert so a flapping endpoint cannot spam. */
const POLLER_ALERT_MIN_INTERVAL_MS = 30_000;

type TickState = Pick<
  PositionRecord,
  | 'tickCount'
  | 'suspectTickCount'
  | 'firstTickAtMs'
  | 'lastTickAtMs'
  | 'forcedReadAtMs'
  | 'entryMoveFromDetectPct'
>;

/** Initial tick accounting for a newly tracked position. */
function freshTickState(): TickState {
  return {
    tickCount: 0,
    suspectTickCount: 0,
    firstTickAtMs: null,
    lastTickAtMs: null,
    forcedReadAtMs: null,
    entryMoveFromDetectPct: null,
  };
}

export class PositionManager {
  private readonly config: Config;
  private readonly bus: TypedBus;
  private readonly repos: Repositories;
  private readonly poller: PricePoller;
  /**
   * Optional PUSH tick source (LaserStream account-subscribe). Additive: the
   * poller keeps its cadence guarantee (time-stops fire on unchanged prices);
   * push ticks add inter-poll freshness for stops, trails and the monitors.
   */
  private readonly ingest: PriceIngest | undefined;
  private readonly log = logger.child({ mod: 'positions' });
  private readonly positions = new Map<Mint, PositionRecord>();
  private readonly pendingEntries = new Set<Mint>();
  private readonly pendingRelaxedEntries = new Set<Mint>();
  private readonly now: () => number;
  /** dry-run/live: builds + broadcasts real buy/sell txs alongside the FSM. */
  private readonly executor: Executor | undefined;
  private readonly exitSupervisor: ExitSupervisor | undefined;
  private readonly risk:
    | {
        canEnter(): { ok: boolean; reason?: string; detail?: string };
        reserveSol?(sol: number): void;
        releaseSol?(sol: number): void;
        applyBalanceDeltaSol?(deltaSol: number): void;
      }
    | undefined;
  private unsubscribe: (() => void) | null = null;
  private unsubscribeKill: (() => void) | null = null;
  private timeStopTimer: NodeJS.Timeout | null = null;
  /** NO_PRICE_DATA exits this session. Past the configured count, stop entering. */
  private blindExits = 0;
  /** Last poller failure/deadline counts seen by the degraded-feed alert. */
  private lastPollFailureCount = 0;
  private lastPollAlertAtMs = 0;

  constructor(deps: {
    config: Config;
    bus: TypedBus;
    repos: Repositories;
    poller: PricePoller;
    ingest?: PriceIngest;
    executor?: Executor;
    risk?: {
      canEnter(): { ok: boolean; reason?: string; detail?: string };
      reserveSol?(sol: number): void;
      releaseSol?(sol: number): void;
      applyBalanceDeltaSol?(deltaSol: number): void;
    };
    now?: () => number;
  }) {
    this.config = deps.config;
    this.bus = deps.bus;
    this.repos = deps.repos;
    this.poller = deps.poller;
    this.ingest = deps.ingest;
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
        {
          ...(e.feedSource !== undefined ? { feedSource: e.feedSource } : {}),
          ...(e.venue !== undefined ? { venue: e.venue } : {}),
          ...(e.entrySoftScore !== undefined ? { entrySoftScore: e.entrySoftScore } : {}),
          ...(e.detectedAtMs !== undefined ? { detectedAtMs: e.detectedAtMs } : {}),
        },
      ),
    );
    this.unsubscribeKill = this.bus.on('killSwitch', () => this.forceCloseAll('KILL_SWITCH', 'kill switch'));
    // Wall-clock backup: PricePoller skips ticks when a request is in flight, so
    // TIME_STOP can miss its window and pin a concurrent slot (seen on 8Ynp…).
    // Also carries the blind-position guard, which needs a much shorter horizon.
    const watchMs = Math.max(this.config.positions.pricePollMs, 1000);
    this.timeStopTimer = setInterval(() => this.runWatchdog(), watchMs);
    this.log.info('position manager started', { mode: this.config.mode });
  }

  stop(): void {
    this.unsubscribe?.();
    this.unsubscribeKill?.();
    this.unsubscribe = null;
    this.unsubscribeKill = null;
    if (this.timeStopTimer) {
      clearInterval(this.timeStopTimer);
      this.timeStopTimer = null;
    }
    for (const rec of this.positions.values()) {
      if (rec.ladderTimer) clearInterval(rec.ladderTimer);
    }
    this.poller.stop();
  }

  /**
   * Timer-driven safety net. The blind guard runs FIRST: it operates on a
   * seconds horizon while TIME_STOP operates on minutes, and a position with no
   * price data cannot be evaluated by anything else.
   */
  private runWatchdog(): void {
    // MUST be this.now() — tests inject a constant clock, and a Date.now() slip
    // here would fire the guard on nearly every live test.
    const now = this.now();
    this.checkPollerHealth(now);
    if (this.config.positions.blindGuardEnabled) this.enforceBlindGuard(now);
    this.enforceTimeStops(now);
  }

  /**
   * Surface a degraded price poller. Every one of these used to be a `debug`
   * log, which is why a feed that starved 14 of 25 positions was invisible.
   */
  private checkPollerHealth(now: number): void {
    const stats = this.poller.pollStats;
    const degraded = stats.failures + stats.deadlineExpired;
    if (degraded <= this.lastPollFailureCount) return;
    this.lastPollFailureCount = degraded;
    if (this.poller.size === 0) return;
    if (now - this.lastPollAlertAtMs < POLLER_ALERT_MIN_INTERVAL_MS) return;
    this.lastPollAlertAtMs = now;
    this.bus.emit('alert', {
      level: 'error',
      message:
        `⚠ price poller degraded — ${stats.failures} failures, ${stats.deadlineExpired} deadline expirations, ` +
        `${stats.overlapSkips} skipped cycles across ${this.poller.size} tracked position(s)` +
        (stats.lastErr ? ` · ${stats.lastErr}` : ''),
      telegram: true,
    });
  }

  /**
   * Close positions the price feed has stopped covering.
   *
   * The exit FSM is driven purely by ticks, so a position with no ticks is
   * unmanaged: it cannot stop out, trail or take profit. Forensics on 25 live
   * positions found 14 that exited on a SINGLE price observation — 0 wins,
   * median -22.6%, and a realized stop-loss mean of -24.9% against a -15%
   * configured stop, because the FSM stopped at the first price it ever saw.
   * `exits.timeStopMinutes` (600_000 ms) is ~100x too slow to catch this.
   *
   * Escalation, per position: no usable tick by `blindFirstTickMs` -> one forced
   * direct read; still nothing by `blindExitMs` -> close at market.
   */
  private enforceBlindGuard(now: number): void {
    const cfg = this.config.positions;
    const blind: Array<[Mint, PositionRecord, number]> = [];

    for (const [mint, rec] of this.positions) {
      if (rec.exiting || rec.pos.state !== 'OPEN') continue;
      // Never ticked: measure from entry, and exit sooner — there is no
      // last-known price to fall back on, so holding is pure exposure.
      // Has ticked: measure from the last tick and allow more rope.
      const everTicked = rec.lastTickAtMs !== null;
      const since = everTicked ? rec.lastTickAtMs! : rec.pos.openedAtMs;
      const silentMs = now - since;
      const exitAfterMs = everTicked ? cfg.blindStaleTickMs : cfg.blindExitMs;

      if (silentMs >= exitAfterMs) {
        blind.push([mint, rec, silentMs]);
        continue;
      }
      if (silentMs >= cfg.blindFirstTickMs && rec.forcedReadAtMs === null) {
        rec.forcedReadAtMs = now;
        this.log.warn('no price tick — forcing a direct vault read', {
          mint,
          silentMs,
          everTicked,
          ticks: rec.tickCount,
          suspectTicks: rec.suspectTickCount,
        });
        void this.forcePriceRead(mint, rec);
      }
    }

    if (blind.length === 0) return;

    // Several positions blind at once means the fault is the poller or the RPC,
    // not the pools. Stop opening new positions BEFORE issuing the exits, so
    // nothing stacks on top of a feed we know is down.
    const systemic = blind.length >= Math.max(2, Math.ceil(this.positions.size / 2));
    if (systemic) {
      this.bus.emit('alert', {
        level: 'error',
        message: `⚠ price feed down — ${blind.length}/${this.positions.size} positions blind, halting entries`,
        telegram: true,
      });
      this.bus.emit('killSwitch', {
        source: 'internal',
        detail: `${blind.length} positions blind simultaneously — pricing pipeline is not delivering ticks`,
      });
    }

    for (const [mint, rec, silentMs] of blind) {
      this.blindExits++;
      this.log.error('no usable price tick — closing at market', {
        mint,
        silentMs,
        ticks: rec.tickCount,
        suspectTicks: rec.suspectTickCount,
      });
      // Deliberately no PnL figure: with no tick we do not have one, and the
      // old wall-clock path announced a fabricated ~0 because lastPrice was
      // still the entry price.
      this.bus.emit('alert', {
        level: 'error',
        message:
          `⚠ exit ${short(mint)} — NO_PRICE_DATA: no usable tick in ${silentMs}ms ` +
          `(${rec.tickCount} ticks, ${rec.suspectTickCount} suspect), closing at market · pnl unknown until fill`,
        telegram: true,
      });
      this.forceCloseAt(mint, rec, 'NO_PRICE_DATA', `no usable price tick in ${silentMs}ms`, now);
    }

    if (!systemic && this.blindExits >= cfg.blindExitKillSwitchCount) {
      this.bus.emit('killSwitch', {
        source: 'internal',
        detail: `${this.blindExits} blind exits this session — pricing pipeline is unreliable`,
      });
    }
  }

  /**
   * One-shot direct vault read for a position the poll loop is not covering.
   * Routes through onTick so the excursions, monitors and FSM all see it via the
   * single existing path.
   */
  private async forcePriceRead(mint: Mint, rec: PositionRecord): Promise<void> {
    try {
      const read = await this.poller.readOnce(rec.pricing);
      if (!read || !(read.price > 0)) {
        this.log.warn('forced vault read returned no usable price', { mint });
        return;
      }
      this.onTick({
        mint,
        price: read.price,
        baseReserve: read.baseReserve,
        quoteReserveLamports: read.quoteReserveLamports,
        atMs: this.now(),
      });
    } catch (err) {
      this.log.warn('forced vault read failed', { mint, err });
    }
  }

  /**
   * Close one position at its last known price. Shared by the wall-clock
   * TIME_STOP and the blind-position guard so both paths behave identically.
   */
  private forceCloseAt(
    mint: Mint,
    rec: PositionRecord,
    trigger: ExitTrigger,
    detail: string,
    now: number,
    onPaperFill?: (fill: Fill) => void,
  ): void {
    if (this.config.mode === 'live' && this.executor) {
      const fill = rec.pos.previewForceClose(rec.lastPrice, trigger);
      if (!fill) return;
      void this.executeExit(rec, fill, true);
      this.bus.emit('exitTriggered', { mint, trigger, detail });
      return;
    }
    const fill = rec.pos.forceClose(rec.lastPrice, now, trigger);
    if (!fill) return;
    rec.fillCount++;
    if (this.executor) void this.executeExit(rec, fill);
    this.bus.emit('exitTriggered', { mint, trigger, detail });
    onPaperFill?.(fill);
    if (this.config.mode !== 'live') this.finalize(mint, rec, rec.lastPrice);
  }

  /**
   * Force TIME_STOP when the poller has not delivered a tick past the hold
   * limit. Uses lastPrice (entry if none). Safe to call on a timer.
   */
  private enforceTimeStops(now: number): void {
    for (const [mint, rec] of this.positions) {
      if (rec.exiting || rec.pos.state !== 'OPEN') continue;
      const limitMs = this.exitCfgFor(rec.relaxedRisk).timeStopMinutes * 60_000;
      if (now - rec.pos.openedAtMs < limitMs) continue;
      this.log.warn('wall-clock time stop — poller did not tick', { mint, heldMs: now - rec.pos.openedAtMs });
      this.forceCloseAt(mint, rec, 'TIME_STOP', 'wall-clock time stop', now, (fill) => {
        this.bus.emit('alert', {
          level: 'info',
          message: `↗ exit ${short(mint)} — TIME_STOP 100% · pnl ${fill.pnlSol >= 0 ? '+' : ''}${fill.pnlSol.toFixed(4)} SOL`,
          telegram: true,
        });
      });
    }
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
        rec.slippageSol += this.paperSellImpact(rec, fill, rec.lastBaseReserve);
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
      rec.slippageSol += this.paperSellImpact(rec, fill, rec.lastBaseReserve);
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
          ...freshTickState(),
          lastBaseReserve: 0n,
          slippageSol: 0,
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
          ...freshTickState(),
          lastBaseReserve: 0n,
          slippageSol: 0,
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
    ctx: OpenContext = {},
  ): Promise<void> {
    if (this.config.mode === 'live' && this.executor) {
      await this.openLive(mint, sizeSol, highVolatility, pricing, momentumWindowMs, relaxedRisk, relaxedReasons, ctx);
      return;
    }
    await this.openPaperLike(mint, sizeSol, highVolatility, pricing, momentumWindowMs, relaxedRisk, relaxedReasons, ctx);
  }

  private canStartEntry(mint: Mint, relaxedRisk: boolean): boolean {
    if (this.positions.has(mint) || this.pendingEntries.has(mint)) return false;
    if (this.positions.size + this.pendingEntries.size >= this.config.risk.maxConcurrentPositions) {
      this.bus.emit('entryVetoed', { mint, reason: 'CIRCUIT_BREAKER', detail: 'max concurrent positions', code: 'MAX_CONCURRENT' });
      this.log.info('entry blocked — max concurrent positions', { mint, cap: this.config.risk.maxConcurrentPositions });
      return false;
    }
    if (relaxedRisk) {
      const openRelaxed = [...this.positions.values()].filter((rec) => rec.relaxedRisk).length;
      const pendingRelaxed = this.pendingRelaxedEntries.size;
      if (openRelaxed + pendingRelaxed >= this.config.guardrails.relaxedRiskMaxOpenPositions) {
        this.bus.emit('entryVetoed', { mint, reason: 'CIRCUIT_BREAKER', detail: 'max relaxed-risk positions', code: 'MAX_RELAXED' });
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
      this.bus.emit('entryVetoed', {
        mint,
        reason,
        detail: `${decision.reason}: ${decision.detail ?? ''}`,
        code: decision.reason === 'KILL_SWITCH' ? 'KILL_SWITCH' : 'RISK_BREAKER',
      });
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
    ctx: OpenContext = {},
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
      const meta = this.entryMeta(mint, highVolatility, ctx);
      const analytics = this.loadAnalyticsForMint(mint, highVolatility, openedAtMs, ctx.detectedAtMs);
      this.positions.set(mint, {
        pos,
        pricing: pricingForPosition,
        fillCount: 0,
        ...freshTickState(),
        lastBaseReserve: pricingForPosition.baseReserve,
        slippageSol: this.config.fees.modelPaperSlippage
          ? buyImpactSol(sizeSol, pricingForPosition.quoteReserveLamports)
          : 0,
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

      this.registerPricing(mint, pricingForPosition);

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
    ctx: OpenContext = {},
  ): Promise<void> {
    if (!this.canStartEntry(mint, relaxedRisk)) return;
    const estimatedEntryPrice = computePrice(pricing.baseReserve, pricing.quoteReserveLamports, pricing.baseDecimals);
    if (estimatedEntryPrice <= 0) {
      this.log.warn('cannot open live — invalid entry price', { mint });
      return;
    }

    // Reserve from the in-memory wallet cache before the send so a concurrent
    // screen cannot size against SOL that is about to leave. No RPC here.
    this.risk?.reserveSol?.(sizeSol);

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
      const buy = await this.executor!.buyAndConfirm(pricing.poolAddress, pricing.baseMint, sizeSol, pricing);
      if (!buy.confirmed || !buy.signature) {
        this.risk?.releaseSol?.(sizeSol);
        this.failLiveEntry(mint, pending, buy, describeBuyFailure(buy), momentumWindowMs, relaxedRisk, relaxedReasons);
        return;
      }
      this.recordEntryLatency(mint, buy);
      const rawBaseAmount = await this.executor!.reconcileTokenBalance(pricing.baseMint, pricing.baseIsToken2022 ?? false);
      if (rawBaseAmount <= 0n) {
        this.risk?.releaseSol?.(sizeSol);
        this.failLiveEntry(mint, pending, buy, 'confirmed buy but wallet has no base tokens', momentumWindowMs, relaxedRisk, relaxedReasons);
        return;
      }
      const entryPrice = entryPriceFromRawAmount(sizeSol, rawBaseAmount, pricing.baseDecimals) ?? estimatedEntryPrice;

      const pos = new PaperPosition({ mint, sizeSol, entryPrice, openedAtMs, highVolatility, cfg: this.exitCfgFor(relaxedRisk) });
      const monitor = new EmergencyMonitor(this.monitorCfgFor(relaxedRisk));
      // buildExitLadder does no network; refresh() does (one getLatestBlockhash
      // per tier). It is deliberately NOT awaited here — see below.
      const ladder = this.executor!.buildExitLadder(pricing.poolAddress, pricing.baseMint);
      const meta = this.entryMeta(mint, highVolatility, ctx);
      const analytics = this.loadAnalyticsForMint(mint, highVolatility, openedAtMs, ctx.detectedAtMs);
      const rec: PositionRecord = {
        pos,
        pricing,
        fillCount: 0,
        ...freshTickState(),
        lastBaseReserve: 0n,
        slippageSol: 0,
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
      // Screening snapshot -> real fill. Wider and more meaningful than
      // execution_json.entry.entryMovePct, which stops at the quote.
      rec.entryMoveFromDetectPct =
        estimatedEntryPrice > 0 ? (entryPrice / estimatedEntryPrice - 1) * 100 : null;
      // Pricing FIRST, before anything that touches the network. The position is
      // already on-chain at this point, and awaiting the initial ladder refresh
      // here left it live but unpriced for ~1-3 s (reconcile + 4 serial
      // getLatestBlockhash) — 25-50% of the life of a position that dies in
      // 3-7 s, and the FSM cannot act on a tick it never received.
      // positions.set must precede registerPricing: onTick early-returns when
      // there is no record, so a tick landing between the two would be dropped.
      this.positions.set(mint, rec);
      this.registerPricing(mint, pricing);
      rec.ladderTimer = setInterval(() => {
        void rec.ladder?.refresh(rec.rawBaseAmount).catch((err) => {
          this.log.warn('exit ladder refresh failed', { mint, err });
        });
      }, this.config.exits.ladderRefreshMs);
      // Fire-and-forget: ExitLadder.isStale() returns true on an empty ladder, so
      // ExitSupervisor.broadcastAttempt falls through to a fresh sellAndConfirm.
      // The await bought exit LATENCY, not exit correctness.
      void ladder.refresh(rawBaseAmount).catch((err) => {
        this.log.warn('initial exit ladder refresh failed — first exit will build fresh', { mint, err });
      });
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
      this.risk?.releaseSol?.(sizeSol);
      // A move-gate skip is a deliberate no-trade, not an execution failure:
      // same FAILED row (nothing was sent) with its own event so it is
      // countable, warn-level, and no error alert.
      const gated = err instanceof EntryMoveExceeded;
      this.persistPosition({ ...pending, state: 'FAILED' }, {
        pricingJson: safeJson(pricing),
        executionJson: safeJson(
          gated
            ? { event: 'entry_move_gate', entryMovePct: err.movePct, capPct: err.capPct }
            : { event: 'entry_exception', error: (err as Error).message },
        ),
        momentumWindowMs,
        relaxedRisk,
        relaxedReasonsJson: relaxedReasons.length ? JSON.stringify(relaxedReasons) : null,
      });
      // Mirror failLiveEntry: without this an entry EXCEPTION is invisible on
      // the bus (the row is persisted FAILED but nothing is emitted), so any
      // subscriber tracking entry outcomes silently misses it.
      this.bus.emit('positionUpdate', { ...pending, state: 'FAILED' });
      if (gated) {
        this.bus.emit('alert', { level: 'warn', message: `⏭ skipped ${short(mint)} — ${err.message}`, telegram: true });
        this.log.warn('live entry skipped by move gate', { mint, entryMovePct: err.movePct, capPct: err.capPct });
      } else {
        this.bus.emit('alert', { level: 'error', message: `live entry failed ${short(mint)} — ${(err as Error).message}`, telegram: true });
        this.log.error('live entry failed', { mint, err });
      }
    } finally {
      this.pendingEntries.delete(mint);
      this.pendingRelaxedEntries.delete(mint);
    }
  }

  private async executeEntry(mint: Mint, pricing: PoolPricingRef, sizeSol: number): Promise<void> {
    try {
      await this.executor!.buy(pricing.poolAddress, pricing.baseMint, sizeSol, pricing);
    } catch (err) {
      if (err instanceof EntryMoveExceeded) this.log.warn('dry-run entry skipped by move gate', { mint, entryMovePct: err.movePct });
      else this.log.error('entry execution failed', { mint, err });
    }
  }

  /**
   * Entry latency samples from a confirmed buy: wall-clock submit→confirm
   * (`entry_confirm`) and chain-relative slots-to-land (`entry_land_slots`).
   */
  private recordEntryLatency(mint: string, buy: BroadcastResult): void {
    // Route attribution: the send path that landed it (jito/primary/secondary).
    const feedSource = buy.landedVia ?? buy.route ?? null;
    try {
      if (buy.confirmLatencyMs !== undefined && buy.confirmLatencyMs > 0) {
        this.repos.recordLatencySample({
          kind: 'entry_confirm',
          latencyMs: buy.confirmLatencyMs,
          mint,
          ...(feedSource ? { feedSource } : {}),
        });
      }
      if (buy.slotsToLand !== undefined) {
        this.repos.recordLatencySample({
          kind: 'entry_land_slots',
          latencyMs: buy.slotsToLand,
          mint,
          ...(feedSource ? { feedSource } : {}),
        });
      }
    } catch (err) {
      this.log.debug('entry latency sample failed', { mint, err });
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

  /**
   * Track a position's pool on the poller (cadence guarantee) and, when wired,
   * the push ingest (freshness). The creator's base-token ATA rides along for
   * the dev-dump monitor.
   */
  private registerPricing(mint: Mint, pricing: PoolPricingRef): void {
    let creatorAta: string | undefined;
    try {
      creatorAta = creatorAtaFor(this.config, pricing);
    } catch (err) {
      this.log.warn('creator ATA derivation failed — dev-dump monitor disabled for this position', { mint, err });
    }
    const ref: PoolRef = {
      mint,
      baseVault: pricing.baseVault,
      quoteVault: pricing.quoteVault,
      baseDecimals: pricing.baseDecimals,
      ...(creatorAta ? { creatorAta } : {}),
    };
    this.poller.register(ref);
    this.ingest?.register(ref, (tick) => this.onTick(tick), {
      baseReserve: pricing.baseReserve,
      quoteReserveLamports: pricing.quoteReserveLamports,
    });
  }

  /** Shared with the dry-run twin so both legs run identical exit rules. */
  private exitCfgFor(relaxedRisk: boolean): Config['exits'] {
    return exitCfgFor(this.config, relaxedRisk);
  }

  private monitorCfgFor(relaxedRisk: boolean): EmergencyMonitorConfig {
    return monitorCfgFor(this.config, relaxedRisk);
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

    /**
     * Suspect-tick guard. `computePrice` returns 0 when baseReserve is 0, so a
     * torn or rolled-back read at `processed` commitment yields a 0 price — and
     * feeding that to the FSM reads as -100% and fires an instant market stop
     * on a garbage read. A suspect tick must never touch lastPrice, the
     * excursions or the FSM. It is also NOT persisted: the dashboard marks
     * positions from the latest price_ticks row, so a 0 there would show a
     * bogus mark.
     */
    const usable = Number.isFinite(tick.price) && tick.price > 0;

    if (usable) {
      rec.tickCount++;
      if (rec.firstTickAtMs === null) rec.firstTickAtMs = tick.atMs;
      rec.lastTickAtMs = tick.atMs;

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
      if (tick.baseReserve > 0n) rec.lastBaseReserve = tick.baseReserve;
      this.updateExcursions(rec, tick.price);
    } else {
      rec.suspectTickCount++;
      this.log.warn('suspect price tick rejected — not fed to the exit FSM', {
        mint: tick.mint,
        price: tick.price,
        baseReserve: tick.baseReserve.toString(),
        suspectTicks: rec.suspectTickCount,
      });
    }

    // In-position emergency check (LP pull / creator dump) — worst-case exit.
    // Runs on EVERY tick including suspect ones: a genuinely drained quote vault
    // is exactly the LP-pull case this monitor exists to catch, and suppressing
    // it would be worse than the bug the suspect guard fixes.
    const signal = rec.monitor.onTick({
      quoteReserveLamports: tick.quoteReserveLamports,
      ...(tick.creatorBaseBalance !== undefined ? { creatorBaseBalance: tick.creatorBaseBalance } : {}),
    });
    if (signal && rec.pos.state === 'OPEN') {
      // On a suspect tick there is no trustworthy price — fall back to the last
      // usable one rather than closing the position at 0.
      this.handleEmergency(tick.mint, rec, usable ? tick.price : rec.lastPrice, signal.kind, signal.detail);
      return;
    }

    if (!usable) return;

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
      rec.slippageSol += this.paperSellImpact(rec, fill, tick.baseReserve);
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
    const fees = this.estimateFees(rec.pos.sizeSol, rec.fillCount) + rec.slippageSol;
    const net = gross - fees;
    const pnlPct = (net / rec.pos.sizeSol) * 100;
    const closedAt = rec.pos.closedAtMs ?? this.now();
    const holdMs = closedAt - rec.pos.openedAtMs;
    this.updateExcursions(rec, exitPrice);

    this.poller.unregister(mint);
    this.ingest?.unregister(mint);
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
      slippageSol: rec.slippageSol,
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
      // Tick accounting: the measurement that exposed the blind-exit bug, and
      // the one that says whether it stays fixed.
      ticksObserved: rec.tickCount,
      suspectTicks: rec.suspectTickCount,
      firstTickMs: rec.firstTickAtMs === null ? null : rec.firstTickAtMs - rec.pos.openedAtMs,
      entryMoveFromDetectPct: rec.entryMoveFromDetectPct,
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
    // Credit proceeds into the in-memory wallet cache (no getBalance).
    this.risk?.applyBalanceDeltaSol?.(fill.fraction * rec.pos.sizeSol + fill.pnlSol);

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
    return estimatePaperFees(sizeSol, exitFills, this.config.fees);
  }

  /**
   * Constant-product impact of a simulated sell. Paper / dry-run only: live
   * fills are applied at their confirmed execution price, which already
   * embeds impact, so charging it again would double count.
   */
  private paperSellImpact(rec: PositionRecord, fill: Fill, baseReserve: bigint): number {
    if (this.config.mode === 'live' || !this.config.fees.modelPaperSlippage) return 0;
    const tokensSold = (rec.pos.sizeSol / rec.pos.entryPrice) * fill.fraction;
    return sellImpactSol(tokensSold * fill.price, tokensSold, baseReserveWhole(baseReserve, rec.pricing.baseDecimals));
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

  /**
   * Attribution for the position row. The openPosition event is authoritative:
   * the graduations row is written by the detector's background confirm loop
   * (up to ~3 s after detection), so a fast screen used to open before it
   * existed and left feed_source/venue NULL on 411/524 rows (F15). The DB
   * lookup is only a fallback for callers that do not carry the event fields.
   */
  private entryMeta(mint: Mint, highVolatility: boolean, ctx: OpenContext = {}): {
    highVolatility: boolean;
    entrySoftScore: number | null;
    feedSource: string | null;
    venue: string | null;
  } {
    let entrySoftScore: number | null = ctx.entrySoftScore ?? null;
    let feedSource: string | null = ctx.feedSource ?? null;
    let venue: string | null = ctx.venue ?? null;
    if (entrySoftScore !== null && feedSource !== null && venue !== null) {
      return { highVolatility, entrySoftScore, feedSource, venue };
    }
    try {
      entrySoftScore ??= this.repos.latestSoftScore(mint);
      const g = this.repos.latestGraduationMeta(mint);
      feedSource ??= g.feedSource;
      venue ??= g.venue;
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
    detectedAtMs?: number,
  ): { features: StrategyFeatureFields; detectToOpenMs: number | null } {
    const session = getActiveRunSession();
    let features: StrategyFeatureFields = {
      sessionId: session?.id ?? null,
      configHash: session?.configHash ?? null,
    };
    // Detection wall-clock from the event; graduations.created_at is only
    // written after the background on-chain confirm and biased detect->open low.
    let detectToOpenMs: number | null =
      detectedAtMs !== undefined ? Math.max(0, openedAtMs - detectedAtMs) : null;
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
        sellabilityStatus: cand.sellabilityStatus,
        poolMovePct: cand.poolMovePct,
        mintAgeMs: cand.mintAgeMs,
        creator: cand.creator,
        mcapSolAtEntry: cand.mcapSolAtEntry,
        feeTierBps: cand.feeTierBps,
        populationOk: cand.populationOk,
      };
      void highVolatility;
      if (detectToOpenMs === null) {
        const gradMs = this.repos.graduationCreatedAtMs(mint);
        if (gradMs !== null) detectToOpenMs = Math.max(0, openedAtMs - gradMs);
      }
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
    ...(f.sellabilityStatus ? { sellabilityStatus: f.sellabilityStatus } : {}),
    ...(f.poolMovePct !== undefined && f.poolMovePct !== null ? { poolMovePct: f.poolMovePct } : {}),
    ...(f.mintAgeMs !== undefined && f.mintAgeMs !== null ? { mintAgeMs: f.mintAgeMs } : {}),
    ...(f.creator ? { creator: f.creator } : {}),
    ...(f.mcapSolAtEntry !== undefined && f.mcapSolAtEntry !== null ? { mcapSolAtEntry: f.mcapSolAtEntry } : {}),
    ...(f.feeTierBps !== undefined && f.feeTierBps !== null ? { feeTierBps: f.feeTierBps } : {}),
    ...(f.populationOk !== undefined && f.populationOk !== null ? { populationOk: f.populationOk } : {}),
  };
}

/** Attribution carried on the openPosition event (see entryMeta). */
export interface OpenContext {
  feedSource?: string;
  venue?: string;
  entrySoftScore?: number;
  detectedAtMs?: number;
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

/**
 * Say WHICH stage of the buy failed. "buy not confirmed" covered both a failed
 * pre-send simulate (returns in < 1 s) and a real 12 s confirmation timeout,
 * which sent 2026-09-17's diagnosis down the wrong path.
 */
export function describeBuyFailure(r: BroadcastResult): string {
  if (!r.sent) {
    if (r.simErr !== undefined && r.simErr !== null) return `buy simulation failed: ${errText(r.simErr)}`;
    return 'buy not sent';
  }
  if (r.sendErr !== undefined && r.sendErr !== null) return `buy sent but not confirmed: ${errText(r.sendErr)}`;
  return 'buy sent but not confirmed';
}

function errText(err: unknown): string {
  if (err instanceof Error) return err.message;
  if (typeof err === 'string') return err;
  try {
    return JSON.stringify(err) ?? String(err);
  } catch {
    return String(err);
  }
}
