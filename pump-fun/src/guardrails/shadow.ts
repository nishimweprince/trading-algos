import type { RpcClient } from '../core/rpc.ts';
import type { Repositories } from '../persistence/repositories.ts';
import type { Mint } from '../core/types.ts';
import { ConfigSchema, type Config } from '../config/schema.ts';
import { PricePoller, type PoolRef, type PriceTick } from '../positions/pricing.ts';
import { PaperPosition } from '../positions/position.ts';
import { estimatePaperFees } from '../positions/paperFees.ts';
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
  verdict: 'veto' | 'accept_not_entered';
  primaryVetoCode: string | null;
  /** Full set of red-flag / veto codes when available. */
  vetoCodes?: string[];
  /** Price at graduation — the hypothetical entry price. Must be > 0. */
  baselinePrice: number;
  poolRef: PoolRef;
  highVolatility?: boolean;
  sessionId?: number | null;
  configHash?: string | null;
}

interface ShadowState {
  req: ShadowTrackRequest;
  pos: PaperPosition;
  peak: number;
  trough: number;
  samples: number;
  fillCount: number;
  startedMs: number;
  lastPrice: number;
}

export interface ShadowTrackerOptions {
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
  now?: () => number;
}

export class ShadowTracker {
  private readonly repos: Repositories;
  private readonly poller: PricePoller;
  private readonly states = new Map<Mint, ShadowState>();
  private readonly windowMs: number;
  private readonly pollMs: number;
  private readonly maxConcurrent: number;
  private readonly sizeSol: number;
  private readonly exits: Config['exits'];
  private readonly fees: Config['fees'];
  private readonly now: () => number;
  private readonly log = logger.child({ mod: 'shadow' });
  private sweepTimer: NodeJS.Timeout | null = null;
  private droppedAtCapacity = 0;

  constructor(rpc: RpcClient, repos: Repositories, opts: ShadowTrackerOptions = {}) {
    this.repos = repos;
    this.windowMs = opts.windowMs ?? 20 * 60_000;
    this.pollMs = opts.pollMs ?? 3_000;
    this.maxConcurrent = opts.maxConcurrent ?? 25;
    this.sizeSol = opts.sizeSol ?? CONFIG_DEFAULTS.entry.baseSizeSol;
    this.exits = opts.exits ?? CONFIG_DEFAULTS.exits;
    this.fees = opts.fees ?? CONFIG_DEFAULTS.fees;
    this.now = opts.now ?? (() => Date.now());
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
    if (this.states.has(req.mint)) return false; // already tracking
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
    const pos = new PaperPosition({
      mint: req.mint,
      sizeSol: this.sizeSol,
      entryPrice: req.baselinePrice,
      openedAtMs,
      highVolatility: req.highVolatility ?? false,
      cfg: this.exits,
    });
    this.states.set(req.mint, {
      req,
      pos,
      peak: req.baselinePrice,
      trough: req.baselinePrice,
      samples: 0,
      fillCount: 0,
      startedMs: openedAtMs,
      lastPrice: req.baselinePrice,
    });
    this.poller.register(req.poolRef);
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
  injectTick(mint: Mint, price: number, atMs?: number): void {
    this.onTick({
      mint,
      price,
      atMs: atMs ?? this.now(),
      baseReserve: 0n,
      quoteReserveLamports: 0n,
    });
  }

  private onTick(tick: PriceTick): void {
    const st = this.states.get(tick.mint);
    if (!st) return;
    if (tick.price > 0) {
      if (tick.price > st.peak) st.peak = tick.price;
      if (tick.price < st.trough) st.trough = tick.price;
      st.samples++;
      st.lastPrice = tick.price;

      // Drive paper exit FSM — never send/broadcast.
      const fills = st.pos.onPrice(tick.price, tick.atMs);
      st.fillCount += fills.length;
    }

    if (st.pos.state === 'CLOSED') {
      this.finish(tick.mint);
      return;
    }
    if (this.now() - st.startedMs >= this.windowMs) this.finish(tick.mint);
  }

  private sweep(): void {
    const cutoff = this.now() - this.windowMs;
    for (const [mint, st] of this.states) {
      if (st.startedMs <= cutoff) this.finish(mint);
    }
  }

  private finish(mint: Mint): void {
    const st = this.states.get(mint);
    if (!st) return;
    this.states.delete(mint);
    this.poller.unregister(mint);

    // Window expired with remainder still open → force-close at last price so
    // we always get realized-style net PnL (not only peak hit rates).
    if (st.pos.state === 'OPEN') {
      const fill = st.pos.forceClose(st.lastPrice, this.now(), 'TIME_STOP');
      if (fill) st.fillCount++;
    }

    const base = st.req.baselinePrice;
    const peakMfePct = (st.peak / base - 1) * 100;
    const maxMaePct = (st.trough / base - 1) * 100;
    const gross = st.pos.realizedPnlSol;
    const fees = estimatePaperFees(st.pos.sizeSol, st.fillCount, this.fees);
    const net = gross - fees;
    const pnlPct = st.pos.sizeSol > 0 ? (net / st.pos.sizeSol) * 100 : 0;
    const closedAt = st.pos.closedAtMs ?? this.now();
    const holdMs = closedAt - st.pos.openedAtMs;
    const trackedMs = this.now() - st.startedMs;

    try {
      this.repos.recordShadowOutcome({
        mint,
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
        sizeSol: st.pos.sizeSol,
        grossPnlSol: gross,
        feesSol: fees,
        netPnlSol: net,
        pnlPct,
        exitReason: st.pos.lastTrigger ?? null,
        holdMs,
        sessionId: st.req.sessionId ?? null,
        configHash: st.req.configHash ?? null,
        outcomeVersion: 'exit_fsm_v1',
      });
      this.log.info('shadow dry-run closed', {
        mint,
        primaryVetoCode: st.req.primaryVetoCode,
        exitReason: st.pos.lastTrigger,
        netPnlSol: Number(net.toFixed(5)),
        peakMfePct: Number(peakMfePct.toFixed(1)),
        holdMs,
      });
    } catch (err) {
      this.log.error('failed to persist shadow outcome', { mint, err });
    }
  }
}
