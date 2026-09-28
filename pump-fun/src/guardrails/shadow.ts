import type { RpcClient } from '../core/rpc.ts';
import type { Repositories } from '../persistence/repositories.ts';
import type { Mint } from '../core/types.ts';
import { ConfigSchema, type Config } from '../config/schema.ts';
import { PricePoller, type PoolRef, type PriceTick } from '../positions/pricing.ts';
import type { PriceIngest } from '../positions/pricing.ts';
import { PaperPosition, type Fill } from '../positions/position.ts';
import { estimatePaperFees, estimatePaperFeesTiered, type FeeLeg } from '../positions/paperFees.ts';
import { FeeModel } from '../positions/feeModel.ts';
import { PendingExit, Simulator } from '../positions/simulator.ts';
import { buyFillPrice, sellProceedsSol } from '../positions/ammImpact.ts';
import { logger } from '../core/logger.ts';

const CONFIG_DEFAULTS = ConfigSchema.parse({});

/**
 * Counterfactual ("shadow") dry-run tracker for candidates we did NOT trade —
 * primarily hard-vetoed candidates. Opens a capital-free paper position at the
 * graduation baseline price and drives the same exit FSM (TPs / trail / stops /
 * time stop) used for non-live accounting, with fee drag, until close.
 *
 * Peak MFE / hit-rate metrics are recorded as a complement. Outcomes land in
 * `shadow_outcomes` (never `positions`), so live capital PnL stays clean.
 *
 * Never broadcasts or sends transactions. Runs on its own PricePoller (separate
 * from the live position poller) at a slower cadence with a hard cap on how many
 * pools it watches at once, so it can never compete with live exit pricing or
 * inflate live risk caps (wallet floor, concurrent live positions, kill switch).
 */
export interface ShadowTrackRequest {
  mint: Mint;
  verdict: 'veto' | 'accept_not_entered' | 'confirm_arm';
  /**
   * Track lane. 'veto' (default) is the counterfactual for a rejected
   * candidate -> shadow_outcomes. `confirm_<ms>` arms (P3.2) are hypothetical
   * delayed entries on canonical graduations -> confirm_outcomes, kept apart
   * so veto-quality stats never pool with them. One mint may run several arms.
   */
  arm?: string;
  primaryVetoCode: string | null;
  /** Full set of red-flag / veto codes when available. */
  vetoCodes?: string[];
  /** Price at graduation — the hypothetical entry price. Must be > 0. */
  baselinePrice: number;
  /** Pool SOL at track time, for constant-product entry/exit impact. Optional. */
  quoteReserveSol?: number | null;
  poolRef: PoolRef;
  highVolatility?: boolean;
  sessionId?: number | null;
  configHash?: string | null;
}

interface ShadowState {
  key: string;
  arm: string;
  req: ShadowTrackRequest;
  /** Null until the deferred honest entry opens (simulator mode). */
  pos: PaperPosition | null;
  /** True while waiting for the first tick at/after entryDueMs. */
  entryPending: boolean;
  entryDueMs: number;
  entryReserveSol: number | null;
  latestQuoteSol: number | null;
  /** Set when the simulated entry fails (slippage / random draw). */
  entryFailed: string | null;
  peak: number;
  trough: number;
  samples: number;
  fillCount: number;
  startedMs: number;
  lastPrice: number;
  entryFeeBps: number;
  exitLegs: FeeLeg[];
  pendingExit?: PendingExit<Fill> | undefined;
  /** Price path for labelling / exit research (P3.4, P3.5). */
  path: Array<{ tMs: number; price: number; quoteReserveSol: number | null }>;
}

export interface ShadowTrackerOptions {
  /**
   * Optional Helius webhook ingest. Tracked pools are mirrored into it for
   * inter-tick freshness; the poller keeps running as the liveness fallback.
   */
  ingest?: PriceIngest;
  /** How long to track each mint (ms). Default 20 min. Caps the dry-run hold. */
  windowMs?: number;
  /** Poll cadence (ms). Default 3 s — slower than the live 1 s poller. */
  pollMs?: number;
  /** Max pools tracked at once. Excess candidates are dropped (logged). */
  maxConcurrent?: number;
  /** Simulated entry size in SOL (for fee-adjusted PnL). Defaults to 0.25. */
  sizeSol?: number;
  /** Exit FSM config — same as live/paper positions. */
  exits?: Config['exits'];
  /** Fee estimates for paper PnL drag. */
  fees?: Config['fees'];
  /** Tiered PumpSwap fees (P1.1); defaults to FeeModel.fromConfig(fees). */
  feeModel?: FeeModel;
  /** Honest simulator (P1.2); disabled when absent. */
  simulator?: Simulator;
  /** Buy slippage bound for the simulated entry draw. Defaults to entry.maxSlippagePct. */
  maxSlippagePct?: number;
  /** Persist each track's tick path to path_ticks (P3.4 labels / P3.5 exit grid). */
  recordPaths?: boolean;
  now?: () => number;
}

export class ShadowTracker {
  private readonly repos: Repositories;
  private readonly poller: PricePoller;
  /** Keyed by `${mint}|${arm}`: one mint can run several arms on one poller registration. */
  private readonly states = new Map<string, ShadowState>();
  private readonly byMint = new Map<Mint, Set<string>>();
  private readonly recordPaths: boolean;
  private readonly windowMs: number;
  private readonly pollMs: number;
  private readonly maxConcurrent: number;
  private readonly sizeSol: number;
  private readonly exits: Config['exits'];
  private readonly fees: Config['fees'];
  private readonly now: () => number;
  private readonly ingest: PriceIngest | null;
  private readonly feeModel: FeeModel;
  private readonly simulator: Simulator | null;
  private readonly maxSlippagePct: number;
  private readonly log = logger.child({ mod: 'shadow' });
  private sweepTimer: NodeJS.Timeout | null = null;
  private droppedAtCapacity = 0;

  constructor(rpc: RpcClient, repos: Repositories, opts: ShadowTrackerOptions = {}) {
    this.repos = repos;
    this.windowMs = opts.windowMs ?? 20 * 60_000;
    this.pollMs = opts.pollMs ?? 3_000;
    this.maxConcurrent = opts.maxConcurrent ?? 25;
    this.sizeSol = opts.sizeSol ?? CONFIG_DEFAULTS.entry.minAbsoluteSol;
    this.exits = opts.exits ?? CONFIG_DEFAULTS.exits;
    this.fees = opts.fees ?? CONFIG_DEFAULTS.fees;
    this.now = opts.now ?? (() => Date.now());
    this.ingest = opts.ingest ?? null;
    this.feeModel = opts.feeModel ?? FeeModel.fromConfig(this.fees);
    this.simulator = opts.simulator?.enabled ? opts.simulator : null;
    this.maxSlippagePct = opts.maxSlippagePct ?? CONFIG_DEFAULTS.entry.maxSlippagePct;
    this.recordPaths = opts.recordPaths ?? false;
    this.poller = new PricePoller(rpc, this.pollMs, this.now);
    this.poller.setHandler((tick) => this.onTick(tick));
  }

  start(): void {
    this.poller.start();
    // A sweeper finishes windows even for pools that stopped producing ticks
    // (e.g. a rugged pool whose vaults read as empty), so states never leak.
    if (!this.sweepTimer) {
      this.sweepTimer = setInterval(() => this.sweep(), this.pollMs);
      this.sweepTimer.unref?.();
    }
  }

  stop(): void {
    this.poller.stop();
    if (this.sweepTimer) clearInterval(this.sweepTimer);
    this.sweepTimer = null;
    this.states.clear();
    this.byMint.clear();
  }

  /** True when this mint/arm is already being tracked. */
  isTracking(mint: Mint, arm = 'veto'): boolean {
    return this.states.has(`${mint}|${arm}`);
  }

  get size(): number {
    return this.states.size;
  }

  /** Bounded concurrent slots — independent of live maxConcurrentPositions. */
  get capacity(): number {
    return this.maxConcurrent;
  }

  noteEligible(mint: Mint): void {
    this.repos.recordShadowCoverage('eligible', mint);
  }

  noteSkippedMissingPricing(mint: Mint): void {
    this.repos.recordShadowCoverage('skipped_missing_pricing', mint);
  }

  track(req: ShadowTrackRequest): boolean {
    if (!(req.baselinePrice > 0)) return false; // can't price without a baseline
    if (!(this.sizeSol > 0)) return false;
    const arm = req.arm ?? 'veto';
    const key = `${req.mint}|${arm}`;
    if (this.states.has(key)) return false; // already tracking
    if (this.states.size >= this.maxConcurrent) {
      this.droppedAtCapacity++;
      this.repos.recordShadowCoverage('dropped_capacity', req.mint);
      // Log periodically so bounded coverage is visible, never silent.
      if (this.droppedAtCapacity % 25 === 1) {
        this.log.warn('shadow tracker at capacity — dropping candidate (coverage bounded)', {
          cap: this.maxConcurrent,
          droppedTotal: this.droppedAtCapacity,
          mint: req.mint,
        });
      }
      return false;
    }
    const openedAtMs = this.now();
    const entryReserveSol = req.quoteReserveSol != null && req.quoteReserveSol > 0 ? req.quoteReserveSol : null;
    // With the honest simulator the entry is deferred: it opens on the first
    // tick at/after entryDueMs, at the impacted fill price, after the
    // entryOutcome draw. Without a simulator the entry opens immediately, but
    // still pays constant-product buy impact when the reserve is known.
    const deferred = this.simulator !== null;
    const immediateEntryPrice =
      entryReserveSol !== null ? buyFillPrice(req.baselinePrice, this.sizeSol, entryReserveSol) : req.baselinePrice;
    const pos = deferred
      ? null
      : new PaperPosition({
        mint: req.mint,
        sizeSol: this.sizeSol,
        entryPrice: immediateEntryPrice,
        openedAtMs,
        highVolatility: req.highVolatility ?? false,
        cfg: this.exits,
      });
    this.states.set(key, {
      key,
      arm,
      req,
      pos,
      entryPending: deferred,
      entryDueMs: deferred ? openedAtMs + this.simulator!.sampleLatencyMs('entry_confirm') : openedAtMs,
      entryReserveSol,
      latestQuoteSol: entryReserveSol,
      entryFailed: null,
      peak: req.baselinePrice,
      trough: req.baselinePrice,
      samples: 0,
      fillCount: 0,
      startedMs: openedAtMs,
      lastPrice: req.baselinePrice,
      entryFeeBps: this.feeModel.forPrice(pos ? pos.entryPrice : req.baselinePrice).bps,
      exitLegs: [],
      path: [],
    });
    const keys = this.byMint.get(req.mint) ?? new Set<string>();
    const firstForMint = keys.size === 0;
    keys.add(key);
    this.byMint.set(req.mint, keys);
    if (firstForMint) {
      this.poller.register(req.poolRef);
      this.ingest?.register(req.poolRef, (tick) => this.onTick(tick));
    }
    this.repos.recordShadowCoverage('started', req.mint);
    this.log.debug('shadow dry-run opened', {
      mint: req.mint,
      primaryVetoCode: req.primaryVetoCode,
      baselinePrice: req.baselinePrice,
      sizeSol: this.sizeSol,
    });
    return true;
  }

  /**
   * Test / operator hook: inject a price tick for a tracked mint without RPC.
   * Drives the same exit FSM as live poller ticks.
   */
  injectTick(mint: Mint, price: number, atMs?: number, quoteReserveSol?: number): void {
    this.onTick({
      mint,
      price,
      atMs: atMs ?? this.now(),
      baseReserve: 0n,
      quoteReserveLamports:
        quoteReserveSol !== undefined ? BigInt(Math.round(quoteReserveSol * 1e9)) : 0n,
    });
  }

  private onTick(tick: PriceTick): void {
    const keys = this.byMint.get(tick.mint);
    if (!keys) return;
    for (const key of [...keys]) {
      const st = this.states.get(key);
      if (st) this.onStateTick(st, tick);
    }
  }

  private onStateTick(st: ShadowState, tick: PriceTick): void {
    if (tick.price > 0) {
      if (this.recordPaths) {
        st.path.push({
          tMs: tick.atMs - st.startedMs,
          price: tick.price,
          quoteReserveSol: tick.quoteReserveLamports > 0n ? Number(tick.quoteReserveLamports) / 1e9 : null,
        });
      }
      if (tick.price > st.peak) st.peak = tick.price;
      if (tick.price < st.trough) st.trough = tick.price;
      st.samples++;
      st.lastPrice = tick.price;
      if (tick.quoteReserveLamports > 0n) st.latestQuoteSol = Number(tick.quoteReserveLamports) / 1e9;

      // Deferred honest entry: open on the first tick at/after entryDueMs.
      // Peak/MFE stay measured from baselinePrice — they describe the coin.
      if (st.entryPending) {
        if (tick.atMs >= st.entryDueMs) this.tryOpenEntry(st, tick);
      }

      // Drive paper exit FSM — never send/broadcast. With the honest
      // simulator an exit fills after a sampled confirm latency (P1.2).
      if (!st.entryPending && st.pos) {
        if (st.pendingExit) {
          if (st.pendingExit.observe(tick.price, tick.atMs)) this.settlePending(st);
        } else if (this.simulator) {
          const trigger = st.pos.previewPriceExit(tick.price, tick.atMs);
          if (trigger) st.pendingExit = new PendingExit(trigger, tick.atMs, this.simulator.sampleLatencyMs('exit_confirm'));
        } else {
          const trigger = st.pos.previewPriceExit(tick.price, tick.atMs);
          if (trigger) {
            const fill = this.effectiveFill(st, trigger);
            st.pos.applyFill(fill, tick.atMs);
            this.recordLeg(st, fill);
          }
        }
      }
    }

    if (st.entryFailed) {
      this.finish(st.key);
      return;
    }
    if (st.pos && st.pos.state === 'CLOSED') {
      this.finish(st.key);
      return;
    }
    if (this.now() - st.startedMs >= this.windowMs) this.finish(st.key);
  }

  /**
   * Open the deferred entry at the impacted fill price, after the
   * entryOutcome draw. A move past the buy slippage bound (or a residual
   * failure draw) ends the track with ENTRY_FAILED and zero PnL.
   */
  private tryOpenEntry(st: ShadowState, tick: PriceTick): void {
    const sim = this.simulator;
    if (!sim) {
      st.entryPending = false;
      return;
    }
    const base = st.req.baselinePrice;
    const movePct = (tick.price / base - 1) * 100;
    const outcome = sim.entryOutcome(movePct, this.maxSlippagePct);
    if (!outcome.ok) {
      st.entryPending = false;
      st.entryFailed = 'ENTRY_FAILED';
      this.log.debug('shadow dry-run entry failed', { mint: st.req.mint, reason: outcome.reason, detail: outcome.detail });
      return;
    }
    const execMid = tick.price * (1 + sim.sampleEntryHaircutPct() / 100);
    const reserve = st.latestQuoteSol ?? st.entryReserveSol;
    const entryPrice = reserve !== null && reserve > 0 ? buyFillPrice(execMid, this.sizeSol, reserve) : execMid;
    st.pos = new PaperPosition({
      mint: st.req.mint,
      sizeSol: this.sizeSol,
      entryPrice,
      openedAtMs: tick.atMs,
      highVolatility: st.req.highVolatility ?? false,
      cfg: this.exits,
    });
    st.entryFeeBps = this.feeModel.forPrice(entryPrice).bps;
    st.entryPending = false;
  }

  /**
   * Reprice an exit fill through the constant-product curve: the mid value
   * `fraction × size × price/entryPrice` sells into the latest tick's pool,
   * so proceeds are always less than the pool's SOL. Unknown reserve falls
   * back to the mid value (today's behaviour).
   */
  private effectiveFill(st: ShadowState, fill: Fill): Fill {
    const pos = st.pos;
    if (!pos) return fill;
    const mid = fill.fraction * pos.sizeSol * (fill.price / pos.entryPrice);
    const reserve = st.latestQuoteSol ?? st.entryReserveSol;
    if (reserve === null || !(reserve > 0) || !(mid > 0)) return fill;
    const proceeds = sellProceedsSol(mid, reserve);
    return pos.repriceFill(fill, fill.price * (proceeds / mid));
  }

  private sweep(): void {
    const now = this.now();
    const cutoff = now - this.windowMs;
    for (const [key, st] of this.states) {
      // A quiet pool must not hold a simulated exit open forever.
      if (st.pendingExit?.isDue(now)) {
        this.settlePending(st);
        if (st.pos && st.pos.state === 'CLOSED') {
          this.finish(key);
          continue;
        }
      }
      if (st.startedMs <= cutoff) this.finish(key);
    }
  }

  private recordLeg(st: ShadowState, fill: Fill): void {
    st.fillCount++;
    const pos = st.pos;
    const mid = pos ? fill.fraction * pos.sizeSol * (fill.price / pos.entryPrice) : 0;
    const reserve = st.latestQuoteSol ?? st.entryReserveSol;
    const valueSol = reserve !== null && reserve > 0 && mid > 0 ? sellProceedsSol(mid, reserve) : mid;
    st.exitLegs.push({
      valueSol,
      feeBps: this.feeModel.forPrice(fill.price).bps,
    });
  }

  private settlePending(st: ShadowState): void {
    const pending = st.pendingExit;
    const pos = st.pos;
    if (!pending || !pos) return;
    st.pendingExit = undefined;
    const fill = this.effectiveFill(st, pos.repriceFill(pending.fill, pending.settlePrice()));
    pos.applyFill(fill, pending.dueAtMs);
    this.recordLeg(st, fill);
  }

  private finish(key: string): void {
    const st = this.states.get(key);
    if (!st) return;
    const mint = st.req.mint;
    this.states.delete(key);
    const keys = this.byMint.get(mint);
    keys?.delete(key);
    if (!keys || keys.size === 0) {
      this.byMint.delete(mint);
      this.poller.unregister(mint);
      this.ingest?.unregister(mint);
    }
    if (this.recordPaths && st.path.length) {
      try {
        this.repos.insertPathTicks(st.path.map((p) => ({ mint, arm: st.arm, ...p })));
      } catch (err) {
        this.log.debug('path tick persist failed', { mint, arm: st.arm, err });
      }
    }

    // Window expired with remainder still open → force-close at last price so
    // we always get realized-style net PnL (not only peak hit rates).
    if (st.pendingExit) this.settlePending(st);
    if (st.pos && st.pos.state === 'OPEN') {
      const preview = st.pos.previewForceClose(st.lastPrice, 'TIME_STOP');
      if (preview) {
        const fill = this.effectiveFill(st, preview);
        st.pos.applyFill(fill, this.now());
        this.recordLeg(st, fill);
      }
    }

    const base = st.req.baselinePrice;
    const peakMfePct = (st.peak / base - 1) * 100;
    const maxMaePct = (st.trough / base - 1) * 100;
    // A failed or never-opened entry carries zero PnL — the coin's peak/MFE
    // above still describe what was missed, not what we earned.
    const gross = st.pos ? st.pos.realizedPnlSol : 0;
    const fees = !st.pos
      ? 0
      : this.feeModel.tierSource === 'flat'
        ? estimatePaperFees(this.sizeSol, st.fillCount, this.fees)
        : estimatePaperFeesTiered({
            entry: { valueSol: this.sizeSol, feeBps: st.entryFeeBps },
            exits: st.exitLegs.length ? st.exitLegs : [{ valueSol: 0, feeBps: 0 }],
            fees: this.fees,
          });
    const net = gross - fees;
    const pnlPct = this.sizeSol > 0 ? (net / this.sizeSol) * 100 : 0;
    const closedAt = st.pos?.closedAtMs ?? this.now();
    const holdMs = st.pos ? closedAt - st.pos.openedAtMs : 0;
    const trackedMs = this.now() - st.startedMs;

    try {
      this.repos.recordShadowOutcome({
        mint,
        arm: st.arm,
        verdict: st.req.verdict,
        primaryVetoCode: st.req.primaryVetoCode,
        vetoCodes: st.req.vetoCodes ?? (st.req.primaryVetoCode ? [st.req.primaryVetoCode] : null),
        baselinePrice: base,
        peakPrice: st.peak,
        troughPrice: st.trough,
        peakMfePct,
        maxMaePct,
        hit25: peakMfePct >= 25,
        hit50: peakMfePct >= 50,
        samples: st.samples,
        trackedMs,
        sizeSol: this.sizeSol,
        grossPnlSol: gross,
        feesSol: fees,
        netPnlSol: net,
        pnlPct,
        exitReason: st.pos?.lastTrigger ?? st.entryFailed ?? null,
        holdMs,
        sessionId: st.req.sessionId ?? null,
        configHash: st.req.configHash ?? null,
        // v3_amm: deferred honest entry (confirm latency + slippage draw) and
        // constant-product entry/exit impact. v2 rows carry flat 0.25 % fees
        // (v1) or tiered fees with mid-price fills (v2/v2_sim) — filter on
        // this column before comparing across the change.
        outcomeVersion: 'exit_fsm_v3_amm',
      });
      this.log.info('shadow dry-run closed', {
        mint,
        primaryVetoCode: st.req.primaryVetoCode,
        exitReason: st.pos?.lastTrigger ?? st.entryFailed ?? null,
        netPnlSol: Number(net.toFixed(5)),
        peakMfePct: Number(peakMfePct.toFixed(1)),
        holdMs,
      });
    } catch (err) {
      this.log.error('failed to persist shadow outcome', { mint, err });
    }
  }
}
