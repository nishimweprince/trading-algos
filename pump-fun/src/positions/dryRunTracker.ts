import type { Config, ExitOverrides } from '../config/schema.ts';
import type { TypedBus } from '../core/bus.ts';
import type { RpcClient } from '../core/rpc.ts';
import type { Repositories } from '../persistence/repositories.ts';
import type { EntryVetoCode, LiveStatus, Mint, PoolPricingRef } from '../core/types.ts';
import { logger } from '../core/logger.ts';
import { getActiveRunSession } from '../core/session.ts';
import { PaperPosition, type Fill } from './position.ts';
import { baseReserveWhole, buyImpactSol, estimatePaperFees, estimatePaperFeesTiered, sellImpactSol, type FeeLeg } from './paperFees.ts';
import { FeeModel } from './feeModel.ts';
import { PendingExit, Simulator } from './simulator.ts';
import { computePrice, PricePoller, type PoolRef, type PriceIngest, type PriceTick } from './pricing.ts';
import { EmergencyMonitor, creatorAtaFor, monitorCfgFor } from './monitors.ts';
import { exitCfgFor } from '../exits/engine.ts';

/**
 * Dry-run TWIN of every ACCEPTED candidate.
 *
 * For each accept, opens an ideal paper position at the same pool mid that
 * `openLive` prices from, then drives an INDEPENDENT exit FSM off its own price
 * ticks. delta(live, dry) is therefore total execution drag — latency, slippage
 * and real fees — which is the number that says whether faster execution
 * recovers the slow-exit bleed.
 *
 * It also covers accepts the live leg never traded (max-concurrent, risk
 * breaker, kill switch, failed entry). Those have no live row at all, so their
 * twin PnL is measured opportunity cost rather than drag.
 *
 * SAFETY INVARIANTS — all four are load-bearing:
 *
 *  1. No send path. Deps are {config, bus, repos, rpc}; there is no Executor and
 *     no Broadcaster, so this class cannot commit capital even by mistake.
 *  2. Writes only to `dry_run_positions`. `positions` is read UNFILTERED by the
 *     kill switch, daily-loss limit, consecutive-loss halt and crash recovery.
 *  3. Emits no bus events. Every `alert` becomes an operator_events row and an
 *     SSE push, and each SSE push triggers a full dashboard refresh against the
 *     same sqlite handle as the trading loop. The twin logs instead.
 *  4. Writes no price_ticks. `latestMarks()` keys by mint only, so twin ticks
 *     would invent mark-to-market for mints with no live position.
 *
 * Its own PricePoller is not a preference but a requirement: PricePoller holds
 * exactly one handler and unregisters by mint, so sharing with the live poller
 * would mean whichever leg finished first silently killed the other's feed.
 */

/** Bus-visible outcomes that resolve what the live leg did with a candidate. */
const CODE_TO_STATUS: Record<EntryVetoCode, LiveStatus> = {
  MAX_CONCURRENT: 'blocked_concurrent',
  MAX_RELAXED: 'blocked_relaxed_cap',
  RISK_BREAKER: 'blocked_breaker',
  KILL_SWITCH: 'blocked_kill_switch',
};

/**
 * `pending_entry` is provisional and may be upgraded; everything else is
 * terminal and first-writer-wins, so a late signal can't rewrite history.
 */
function isTerminalStatus(status: LiveStatus): boolean {
  return status !== 'pending_entry' && status !== 'unknown';
}

interface Attribution {
  status: LiveStatus;
  detail?: string | undefined;
  atMs: number;
}

interface TwinState {
  pos: PaperPosition;
  poolRef: PoolRef;
  /** Same LP-pull / creator-dump defence live runs — see monitorCfgFor. */
  monitor: EmergencyMonitor;
  openedAtMs: number;
  peak: number;
  trough: number;
  lastPrice: number;
  /** Reserves on the most recent tick, for sell impact on force-closes. */
  lastBaseReserve: bigint;
  samples: number;
  fillCount: number;
  /** Modelled constant-product impact (entry + every exit fill), in SOL. */
  slippageSol: number;
  /** Tick timestamp → FSM decision on the fill that closed the twin. */
  exitTriggerToConfirmMs: number | null;
  timeToMfeMs: number | null;
  timeToMaeMs: number | null;
  highVolatility: boolean;
  relaxedRisk: boolean;
  attribution: Attribution;
  feedSource: string | null;
  venue: string | null;
  entrySoftScore: number | null;
  /** Tiered fees (P1.1). */
  entryFeeBps: number;
  exitLegs: FeeLeg[];
  /** Honest simulator exit in flight (P1.2). */
  pendingExit?: PendingExit<Fill> | undefined;
  pendingTimer?: NodeJS.Timeout | undefined;
  lastFillPrice: number | null;
}

interface AcceptMeta {
  feedSource?: string | undefined;
  venue?: string | undefined;
  entrySoftScore?: number | undefined;
}

export interface DryRunTrackerDeps {
  config: Config;
  bus: TypedBus;
  repos: Repositories;
  rpc: RpcClient;
  now?: () => number;
  /**
   * Optional push tick source (Helius webhook or LaserStream account
   * subscribe). Tracked pools are mirrored into it for inter-tick freshness;
   * the poller keeps running as the liveness fallback.
   */
  ingest?: PriceIngest;
  feeModel?: FeeModel;
  /** Own PRNG stream so the twin's draws never perturb the primary leg's. */
  simulator?: Simulator;
}

export class DryRunTracker {
  private readonly config: Config;
  private readonly bus: TypedBus;
  private readonly repos: Repositories;
  private readonly poller: PricePoller;
  private readonly ingest: PriceIngest | null;
  /** Experiment lane: exit-rule overrides applied to the twin only. */
  private readonly exitOverrides: ExitOverrides | null;
  private readonly exitOverridesJson: string | null;
  private readonly now: () => number;
  private readonly log = logger.child({ mod: 'dryrun' });
  private readonly feeModel: FeeModel;
  private readonly simulator: Simulator;

  private readonly states = new Map<Mint, TwinState>();
  /**
   * Attribution that arrived before (or without) a twin state.
   *
   * This is not defensive padding — it is the primary path for blocked entries.
   * `bus.emit` dispatches synchronously in registration order, and
   * PositionManager's handler reaches `canStartEntry` (which re-emits
   * `entryVetoed`) before its first await. Registering this tracker first fixes
   * the common case; this buffer covers the rest, including candidates whose
   * pricing fallback made the open async.
   */
  private readonly pending = new Map<Mint, Attribution>();

  private readonly windowMs: number;
  private readonly pollMs: number;
  private readonly maxConcurrent: number;
  private readonly defaultStatus: LiveStatus;

  private sweepTimer: NodeJS.Timeout | null = null;
  private unsubscribe: Array<() => void> = [];
  private droppedAtCapacity = 0;

  constructor(deps: DryRunTrackerDeps) {
    this.config = deps.config;
    this.bus = deps.bus;
    this.repos = deps.repos;
    this.now = deps.now ?? (() => Date.now());
    this.feeModel = deps.feeModel ?? FeeModel.fromConfig(deps.config.fees);
    this.simulator = deps.simulator ?? new Simulator({ ...deps.config.simulator, seed: deps.config.simulator.seed + 1 });

    const twin = deps.config.dryRunTwin;
    this.windowMs = twin.windowMinutes * 60_000;
    this.pollMs = twin.pollMs ?? deps.config.positions.pricePollMs;
    this.maxConcurrent = twin.maxConcurrent;
    // Without a live executor there is no live leg to compare against; the twin
    // still runs (it is the only leg), but the rows must say so.
    this.defaultStatus = deps.config.mode === 'live' ? 'unknown' : 'no_executor';

    if (this.pollMs !== deps.config.positions.pricePollMs) {
      this.log.warn(
        'dry-run twin poll cadence differs from live — delta will conflate poller cadence with execution drag',
        { twinPollMs: this.pollMs, livePollMs: deps.config.positions.pricePollMs },
      );
    }

    this.poller = new PricePoller(deps.rpc, this.pollMs, this.now);
    this.poller.setHandler((tick) => this.onTick(tick));
    this.ingest = deps.ingest ?? null;

    const overrides = twin.exitOverrides ? definedEntries(twin.exitOverrides) : {};
    this.exitOverrides = Object.keys(overrides).length ? (overrides as ExitOverrides) : null;
    this.exitOverridesJson = this.exitOverrides ? JSON.stringify(this.exitOverrides) : null;
  }

  /**
   * MUST be called before PositionManager.start(). Bus dispatch is synchronous
   * and in registration order, so registering second means every concurrency /
   * breaker block is emitted before this tracker has even seen the accept.
   */
  start(): void {
    this.poller.start();
    if (this.exitOverrides) {
      this.log.warn(
        'dry-run twin is running EXIT OVERRIDES — Δ(live, dry) now includes a strategy difference, not execution drag alone',
        { exitOverrides: this.exitOverrides },
      );
    }
    this.unsubscribe.push(
      this.bus.on('openPosition', (e) =>
        this.onAccept(e.mint, e.sizeSol, e.highVolatility, e.relaxedRisk ?? false, e.pricing, {
          feedSource: e.feedSource,
          venue: e.venue,
          entrySoftScore: e.entrySoftScore,
        }),
      ),
      this.bus.on('entryVetoed', (e) => {
        // GUARDRAIL is the pre-accept veto — those go to ShadowTracker and never
        // have a twin, so recording them here would mislabel unrelated mints.
        if (!e.code) return;
        this.attribute(e.mint, CODE_TO_STATUS[e.code], e.detail);
      }),
      this.bus.on('positionUpdate', (p) => {
        if (p.state === 'PENDING_ENTRY') this.attribute(p.mint, 'pending_entry');
        else if (p.state === 'OPEN') this.attribute(p.mint, 'entered');
        else if (p.state === 'FAILED') this.attribute(p.mint, 'entry_failed');
      }),
    );

    if (!this.sweepTimer) {
      // Finishes windows for pools that stopped ticking (rugged pool, dead
      // vault), so states can never leak.
      this.sweepTimer = setInterval(() => this.sweep(), this.pollMs);
      this.sweepTimer.unref?.();
    }
    this.log.info('dry-run twin tracker started', {
      pollMs: this.pollMs,
      maxConcurrent: this.maxConcurrent,
      windowMinutes: this.config.dryRunTwin.windowMinutes,
      sizeMode: this.config.dryRunTwin.sizeMode,
      modelSlippage: this.config.fees.modelPaperSlippage,
      exitOverrides: this.exitOverrides ?? 'none',
    });
  }

  /** Flush every open twin so a restart never loses the dry leg of a live trade. */
  stop(): void {
    for (const unsub of this.unsubscribe) unsub();
    this.unsubscribe = [];
    for (const mint of [...this.states.keys()]) this.finish(mint, 'KILL_SWITCH');
    this.poller.stop();
    if (this.sweepTimer) clearInterval(this.sweepTimer);
    this.sweepTimer = null;
    this.pending.clear();
  }

  get size(): number {
    return this.states.size;
  }

  get capacity(): number {
    return this.maxConcurrent;
  }

  /**
   * Test / operator hook: drive the FSM without RPC. Reserves default to 0n
   * (unknown), which the monitor and slippage model both treat as "no data".
   */
  injectTick(
    mint: Mint,
    price: number,
    atMs?: number,
    extra?: { baseReserve?: bigint; quoteReserveLamports?: bigint; creatorBaseBalance?: bigint },
  ): void {
    this.onTick({
      mint,
      price,
      atMs: atMs ?? this.now(),
      baseReserve: extra?.baseReserve ?? 0n,
      quoteReserveLamports: extra?.quoteReserveLamports ?? 0n,
      ...(extra?.creatorBaseBalance !== undefined ? { creatorBaseBalance: extra.creatorBaseBalance } : {}),
    });
  }

  /**
   * Runs INSIDE the live entry dispatch, so everything here must be cheap and
   * must never throw: an exception would propagate back into `requestOpen` and
   * be logged as "screening failed" for a screen that actually succeeded.
   */
  private onAccept(
    mint: Mint,
    sizeSol: number,
    highVolatility: boolean,
    relaxedRisk: boolean,
    pricing: PoolPricingRef,
    meta: AcceptMeta = {},
  ): void {
    try {
      if (!this.config.dryRunTwin.enabled) return;
      this.coverage('eligible', mint);

      if (this.states.has(mint)) {
        this.coverage('duplicate', mint);
        return;
      }
      if (this.states.size >= this.maxConcurrent) {
        this.droppedAtCapacity++;
        this.coverage('dropped_capacity', mint, `cap=${this.maxConcurrent}`);
        if (this.droppedAtCapacity % 25 === 1) {
          this.log.warn('dry-run twin at capacity — dropping candidate (delta coverage bounded)', {
            cap: this.maxConcurrent,
            droppedTotal: this.droppedAtCapacity,
            mint,
          });
        }
        return;
      }

      const twinSize = this.sizeFor(sizeSol);
      if (!(twinSize > 0)) return;

      // Same value openLive uses as its estimated entry price. Deliberately NOT
      // a fresh readOnce: that would cost an RPC round-trip on the accept hot
      // path and make entryDrag a measure of two reads taken at different
      // milliseconds rather than of execution.
      const entryPrice = computePrice(pricing.baseReserve, pricing.quoteReserveLamports, pricing.baseDecimals);
      if (!(entryPrice > 0)) {
        // Undecodable/empty vaults on the event. One async fallback read, then
        // give up and record the coverage gap rather than mispricing the twin.
        void this.openWithFallbackPrice(mint, twinSize, highVolatility, relaxedRisk, pricing, meta);
        return;
      }

      this.open(mint, twinSize, entryPrice, highVolatility, relaxedRisk, pricing, meta);
    } catch (err) {
      this.log.error('dry-run twin open failed', { mint, err });
    }
  }

  private async openWithFallbackPrice(
    mint: Mint,
    twinSize: number,
    highVolatility: boolean,
    relaxedRisk: boolean,
    pricing: PoolPricingRef,
    meta: AcceptMeta,
  ): Promise<void> {
    try {
      const fresh = await this.poller.readOnce({
        baseVault: pricing.baseVault,
        quoteVault: pricing.quoteVault,
        baseDecimals: pricing.baseDecimals,
      });
      if (!fresh || !(fresh.price > 0)) {
        this.coverage('skipped_missing_pricing', mint);
        return;
      }
      if (this.states.has(mint)) return;
      this.open(mint, twinSize, fresh.price, highVolatility, relaxedRisk, {
        ...pricing,
        baseReserve: fresh.baseReserve,
        quoteReserveLamports: fresh.quoteReserveLamports,
      }, meta);
    } catch (err) {
      this.coverage('skipped_missing_pricing', mint);
      this.log.debug('dry-run twin fallback pricing failed', { mint, err });
    }
  }

  private open(
    mint: Mint,
    sizeSol: number,
    entryPrice: number,
    highVolatility: boolean,
    relaxedRisk: boolean,
    pricing: PoolPricingRef,
    meta: AcceptMeta,
  ): void {
    const openedAtMs = this.now();
    // Honest simulator: the twin enters at the accept snapshot (it has no
    // send path and must not await on the synchronous live dispatch), but
    // pays the same adverse entry haircut the primary paper leg does.
    if (this.simulator.enabled) entryPrice *= 1 + this.simulator.sampleEntryHaircutPct() / 100;
    const pos = new PaperPosition({
      mint,
      sizeSol,
      entryPrice,
      openedAtMs,
      highVolatility,
      // Identical exit rules to live — otherwise the delta would fold a strategy
      // difference into a number read as execution cost. The one sanctioned
      // exception is the experiment lane (dryRunTwin.exitOverrides), which is
      // logged at start and stamped on every row.
      cfg: exitCfgFor(this.config, relaxedRisk, this.exitOverrides ?? undefined),
    });
    let creatorAta: string | undefined;
    try {
      creatorAta = creatorAtaFor(this.config, pricing);
    } catch (err) {
      this.log.debug('creator ATA derivation failed — twin dev-dump monitor off for this position', { mint, err });
    }
    const poolRef: PoolRef = {
      mint,
      baseVault: pricing.baseVault,
      quoteVault: pricing.quoteVault,
      baseDecimals: pricing.baseDecimals,
      ...(creatorAta ? { creatorAta } : {}),
    };

    // Drain any attribution that raced ahead of this open (the normal path for
    // a concurrency/breaker block).
    const pending = this.pending.get(mint);
    this.pending.delete(mint);

    this.states.set(mint, {
      pos,
      poolRef,
      monitor: new EmergencyMonitor(monitorCfgFor(this.config, relaxedRisk)),
      openedAtMs,
      peak: entryPrice,
      trough: entryPrice,
      lastPrice: entryPrice,
      lastBaseReserve: pricing.baseReserve,
      samples: 0,
      fillCount: 0,
      // Buy impact at entry, from the reserves the entry was priced off.
      slippageSol: this.config.fees.modelPaperSlippage ? buyImpactSol(sizeSol, pricing.quoteReserveLamports) : 0,
      exitTriggerToConfirmMs: null,
      timeToMfeMs: null,
      timeToMaeMs: null,
      highVolatility,
      relaxedRisk,
      attribution: pending ?? { status: this.defaultStatus, atMs: openedAtMs },
      feedSource: meta.feedSource ?? null,
      venue: meta.venue ?? null,
      entrySoftScore: meta.entrySoftScore ?? null,
      entryFeeBps: this.feeModel.forPrice(entryPrice).bps,
      exitLegs: [],
      lastFillPrice: null,
    });
    this.poller.register(poolRef);
    this.ingest?.register(poolRef, (tick) => this.onTick(tick), {
      baseReserve: pricing.baseReserve,
      quoteReserveLamports: pricing.quoteReserveLamports,
    });

    // Deferred: this runs inside the synchronous live entry dispatch, so no
    // sqlite write may sit on that path.
    this.coverage('started', mint);
    setImmediate(() => {
      try {
        this.persist(mint, 'OPEN');
      } catch (err) {
        this.log.error('failed to persist dry-run twin open', { mint, err });
      }
    });

    this.log.debug('dry-run twin opened', { mint, sizeSol, entryPrice, relaxedRisk });
  }

  /** Twin notional. `mirror` keeps delta directly interpretable as SOL cost. */
  private sizeFor(liveSizeSol: number): number {
    const twin = this.config.dryRunTwin;
    if (twin.sizeMode === 'fixed') return twin.sizeSol ?? this.config.entry.minAbsoluteSol;
    return liveSizeSol;
  }

  private attribute(mint: Mint, status: LiveStatus, detail?: string): void {
    const atMs = this.now();
    const st = this.states.get(mint);
    if (!st) {
      // No twin yet (or already closed). Buffer it — sweep() prunes stale
      // entries so this map cannot grow without bound.
      const existing = this.pending.get(mint);
      if (existing && isTerminalStatus(existing.status)) return;
      this.pending.set(mint, { status, detail, atMs });
      return;
    }
    if (isTerminalStatus(st.attribution.status)) return; // first terminal writer wins
    st.attribution = { status, detail, atMs };
  }

  private onTick(tick: PriceTick): void {
    const st = this.states.get(tick.mint);
    if (!st) return;

    if (tick.price > 0) {
      if (tick.price > st.peak) {
        st.peak = tick.price;
        st.timeToMfeMs = tick.atMs - st.openedAtMs;
      }
      if (tick.price < st.trough) {
        st.trough = tick.price;
        st.timeToMaeMs = tick.atMs - st.openedAtMs;
      }
      st.samples++;
      st.lastPrice = tick.price;
      if (tick.baseReserve > 0n) st.lastBaseReserve = tick.baseReserve;

      // Same in-position defence as live (LP pull / creator dump). Runs before
      // the FSM: a rug the twin "survived" would otherwise be booked as live
      // execution drag.
      const signal = st.monitor.onTick({
        quoteReserveLamports: tick.quoteReserveLamports,
        ...(tick.creatorBaseBalance !== undefined ? { creatorBaseBalance: tick.creatorBaseBalance } : {}),
      });
      // Honest simulator: an exit in flight only observes until it confirms.
      if (st.pendingExit) {
        if (st.pendingExit.observe(tick.price, tick.atMs)) this.settlePending(tick.mint, st);
        return;
      }

      if (signal && st.pos.state === 'OPEN') {
        this.log.warn('dry-run twin emergency exit', { mint: tick.mint, kind: signal.kind, detail: signal.detail });
        if (this.simulator.enabled) {
          const trigger = st.pos.previewForceClose(tick.price, 'EMERGENCY_EXIT');
          if (trigger) this.beginPending(tick.mint, st, trigger, tick.atMs);
          return;
        }
        st.exitTriggerToConfirmMs = Math.max(0, this.now() - tick.atMs);
        this.finish(tick.mint, 'EMERGENCY_EXIT');
        return;
      }

      // Independent exit FSM — the twin exits when its own rules fire, never
      // mirroring live. That independence is what isolates slow-exit bleed.
      if (this.simulator.enabled) {
        const trigger = st.pos.previewPriceExit(tick.price, tick.atMs);
        if (trigger) {
          this.beginPending(tick.mint, st, trigger, tick.atMs);
          return;
        }
      } else {
        const fills = st.pos.onPrice(tick.price, tick.atMs);
        for (const fill of fills) this.applyTwinFill(st, fill, tick.baseReserve);
        if (fills.length) st.exitTriggerToConfirmMs = Math.max(0, this.now() - tick.atMs);
      }
    }

    if (st.pos.state === 'CLOSED') {
      this.finish(tick.mint);
      return;
    }
    if (this.now() - st.openedAtMs >= this.windowMs) this.finish(tick.mint);
  }

  private applyTwinFill(st: TwinState, fill: Fill, baseReserve: bigint): void {
    st.fillCount++;
    st.lastFillPrice = fill.price;
    st.exitLegs.push({
      valueSol: fill.fraction * st.pos.sizeSol * (fill.price / st.pos.entryPrice),
      feeBps: this.feeModel.forPrice(fill.price).bps,
    });
    st.slippageSol += this.sellImpact(st, fill.fraction, fill.price, baseReserve);
  }

  private beginPending(mint: Mint, st: TwinState, fill: Fill, triggerAtMs: number): void {
    const latencyMs = this.simulator.sampleLatencyMs('exit_confirm');
    st.pendingExit = new PendingExit(fill, triggerAtMs, latencyMs);
    st.pendingTimer = setTimeout(() => {
      if (st.pendingExit && this.states.get(mint) === st) this.settlePending(mint, st);
    }, Math.max(0, triggerAtMs + latencyMs - this.now()));
    st.pendingTimer.unref?.();
  }

  private settlePending(mint: Mint, st: TwinState): void {
    const pending = st.pendingExit;
    if (!pending) return;
    st.pendingExit = undefined;
    if (st.pendingTimer) clearTimeout(st.pendingTimer);
    st.pendingTimer = undefined;
    const fill = st.pos.repriceFill(pending.fill, pending.settlePrice());
    st.pos.applyFill(fill, pending.dueAtMs);
    this.applyTwinFill(st, fill, st.lastBaseReserve);
    st.exitTriggerToConfirmMs = pending.latencyMs;
    if (st.pos.state === 'CLOSED') this.finish(mint);
  }

  private sweep(): void {
    const now = this.now();
    for (const [mint, st] of this.states) {
      if (now - st.openedAtMs >= this.windowMs) this.finish(mint);
    }
    // Attribution for candidates that never opened a twin (missing pricing,
    // capacity drop) would otherwise accumulate forever.
    for (const [mint, att] of this.pending) {
      if (now - att.atMs >= this.windowMs) this.pending.delete(mint);
    }
  }

  private finish(mint: Mint, forcedTrigger: 'TIME_STOP' | 'KILL_SWITCH' | 'EMERGENCY_EXIT' = 'TIME_STOP'): void {
    const st = this.states.get(mint);
    if (!st) return;
    this.states.delete(mint);
    this.poller.unregister(mint);
    this.ingest?.unregister(mint);

    // Window expired (or emergency) with a remainder still open → force-close
    // at the last price so every twin yields realized-style net PnL, not just
    // peak stats.
    if (st.pendingTimer) clearTimeout(st.pendingTimer);
    if (st.pos.state === 'OPEN') {
      const fill = st.pos.forceClose(st.lastPrice, this.now(), forcedTrigger);
      if (fill) this.applyTwinFill(st, fill, st.lastBaseReserve);
    }

    try {
      this.persist(mint, 'CLOSED', st);
      this.log.info('dry-run twin closed', {
        mint,
        liveStatus: st.attribution.status,
        exitReason: st.pos.lastTrigger,
        netPnlSol: Number(this.netPnl(st).toFixed(5)),
        holdMs: (st.pos.closedAtMs ?? this.now()) - st.pos.openedAtMs,
      });
    } catch (err) {
      this.log.error('failed to persist dry-run twin outcome', { mint, err });
    }
  }

  /** Constant-product impact of selling `fraction` of the position at `price`. */
  private sellImpact(st: TwinState, fraction: number, price: number, baseReserve: bigint): number {
    if (!this.config.fees.modelPaperSlippage) return 0;
    const tokensSold = (st.pos.sizeSol / st.pos.entryPrice) * fraction;
    const fillValueSol = tokensSold * price;
    return sellImpactSol(fillValueSol, tokensSold, baseReserveWhole(baseReserve, st.poolRef.baseDecimals));
  }

  /** Same fee model as PositionManager.estimateFees, so paper Δ stays zero. */
  private feesFor(st: TwinState): number {
    if (this.feeModel.tierSource === 'flat') {
      return estimatePaperFees(st.pos.sizeSol, st.fillCount, this.config.fees) + st.slippageSol;
    }
    return (
      estimatePaperFeesTiered({
        entry: { valueSol: st.pos.sizeSol, feeBps: st.entryFeeBps },
        exits: st.exitLegs.length ? st.exitLegs : [{ valueSol: 0, feeBps: 0 }],
        fees: this.config.fees,
      }) + st.slippageSol
    );
  }

  private netPnl(st: TwinState): number {
    return st.pos.realizedPnlSol - this.feesFor(st);
  }

  private persist(mint: Mint, state: 'OPEN' | 'CLOSED', closed?: TwinState): void {
    const st = closed ?? this.states.get(mint);
    if (!st) return;
    const session = getActiveRunSession();
    const base = {
      mint,
      state,
      liveStatus: st.attribution.status,
      liveStatusDetail: st.attribution.detail ?? null,
      liveStatusAtMs: st.attribution.atMs - st.openedAtMs,
      sizeSol: st.pos.sizeSol,
      entryPrice: st.pos.entryPrice,
      openedAt: st.pos.openedAtMs,
      samples: st.samples,
      fillCount: st.fillCount,
      highVolatility: st.highVolatility,
      relaxedRisk: st.relaxedRisk,
      sessionId: session?.id ?? null,
      configHash: session?.configHash ?? null,
      mode: this.config.mode,
      feedSource: st.feedSource,
      venue: st.venue,
      entrySoftScore: st.entrySoftScore,
      exitOverridesJson: this.exitOverridesJson,
      feeTierBps: st.entryFeeBps,
      mcapSolAtEntry: this.feeModel.forPrice(st.pos.entryPrice).mcapSol,
      simulated: this.simulator.enabled,
    };

    if (state === 'OPEN') {
      this.repos.upsertDryRunPosition(base);
      return;
    }

    const gross = st.pos.realizedPnlSol;
    const fees = this.feesFor(st);
    const net = gross - fees;
    const closedAt = st.pos.closedAtMs ?? this.now();
    const entry = st.pos.entryPrice;
    this.repos.upsertDryRunPosition({
      ...base,
      exitPrice: st.lastFillPrice ?? st.lastPrice,
      exitReason: st.pos.lastTrigger ?? null,
      closedAt,
      grossPnlSol: gross,
      feesSol: fees,
      netPnlSol: net,
      pnlPct: st.pos.sizeSol > 0 ? (net / st.pos.sizeSol) * 100 : 0,
      mfePct: entry > 0 ? (st.peak / entry - 1) * 100 : 0,
      maePct: entry > 0 ? (st.trough / entry - 1) * 100 : 0,
      timeToMfeMs: st.timeToMfeMs,
      timeToMaeMs: st.timeToMaeMs,
      holdMs: closedAt - st.pos.openedAtMs,
      slippageSol: st.slippageSol,
      exitTriggerToConfirmMs: st.exitTriggerToConfirmMs,
    });
  }

  /**
   * Deferred: coverage is recorded from `onAccept`, which runs inside the
   * synchronous live entry dispatch. No sqlite write may sit on that path.
   */
  private coverage(
    kind: 'eligible' | 'started' | 'skipped_missing_pricing' | 'dropped_capacity' | 'duplicate',
    mint: Mint,
    detail?: string,
  ): void {
    setImmediate(() => {
      try {
        this.repos.recordDryRunCoverage(kind, mint, detail);
      } catch (err) {
        this.log.debug('failed to record dry-run coverage', { kind, mint, err });
      }
    });
  }
}

function definedEntries(o: object): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(o)) if (v !== undefined) out[k] = v;
  return out;
}
