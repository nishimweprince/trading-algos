/**
 * First-position paper tracker. Each landed migration arrives with two entry
 * marks priced from the migrate tx itself (see migrateTx.ts):
 *
 *   open  — the pool as created, before the caller's bundled buy;
 *   reach — the pool after the migrate tx, the best a non-migrator gets.
 *
 * plus realistic slot-delayed entries (`slot1`.. — see slotEntry.ts), which
 * are also charged `liveCostSol` per round trip (priority fees, tips, network
 * fees: what a live bot pays that the pool never sees).
 *
 * Re-marks the pool at fixed horizons and records a full round trip for
 * each entry: tiered PumpSwap fees both ways and constant-product impact on
 * the entry and exit depth. Zero sends; this tracker owns fixed-horizon exits,
 * while pathExit.ts evaluates ordered stream states separately.
 *
 * The sink runs once at entry (all horizons empty) and again as each horizon
 * is marked, so results are visible as they land instead of after the last
 * horizon; the persistence side upserts by mint.
 *
 * Honesty rules:
 *  - A rejected open mark is NULL with a reason, never a default: a backed-out
 *    open that fails markChecks.ts (drain, inversion, slot drift) scores no
 *    leg rather than a fantasy leg.
 *  - Realistic legs also print a `{leg}_adj` expectancy leg: the conditional
 *    value times the trailing fill probability, minus failed-attempt fees.
 *    Base legs stay conditional-on-fill; `_adj` is what a live sender expects.
 *  - A pool that is gone (account closed) scores −100% for every entry: a rug
 *    is a total loss, never a dropped sample (dropping it biased medians up).
 *    A sell is also capped by the SOL really in the quote vault.
 *  - Realistic legs exit where a sell would LAND: the pool `exitLandMs` after
 *    the horizon (send → land ≈ 1 slot, measured live), not the horizon mid.
 *    open / reach keep the horizon mid (they are idealised by definition).
 *  - Realistic legs pay `liveCostSol`, replaced by the measured cost of that
 *    migration's +1-slot buyers when it arrives (setLiveCost).
 */
import { roundTripGate, roundTripParts, type GateReason, type PoolMark } from './economics.ts';
import { adjustLegForFill, type LegFill } from './fills.ts';
import type { EntryQuality } from './slotEntry.ts';

export interface HorizonResult {
  sec: number;
  price: number | null;
  quoteSol: number | null;
  pnlOpenPct: number | null;
  pnlReachPct: number | null;
  /** Round trip % per entry name (open, reach, slot1, …); realistic legs net of liveCostSol. */
  pnl: Record<string, number | null>;
  /** Mid where the realistic sells land (exitLandMs after the horizon). */
  exitPrice?: number | null;
  exitQuoteSol?: number | null;
  exitRealQuoteSol?: number | null;
  /** Wall-clock observation time of the simulated landed exit. */
  exitAtMs?: number;
  /** The pool was gone at this horizon: every entry scored −100%. */
  rugged?: boolean;
  /** Where this horizon was priced: stream path states, or the RPC fallback. */
  source?: 'path' | 'rpc';
  /** Path fills: 'next-slot' or 'unchanged' (no trade after the decision). */
  fill?: string;
  /** Legs the live bot could not have filled at this size (roundTripGate); their pnl is null. */
  gateReasons?: Record<string, GateReason>;
}

export interface PaperEntry {
  poolAddress: string;
  slot: number | null;
  /** Per-track size override (wallet-sized); defaults to the constructor size. */
  sizeSol?: number;
  /** Real wallet balance this track was sized from; null when unreadable. */
  walletBalanceSol?: number | null;
  /** Why this size (src/racer/walletSize.ts); null for legacy fixed-size tracks. */
  sizingReason?: string | null;
  open: PoolMark | null;
  /** Open-mark verdict from markChecks.ts; null when the open was unknown by derivation. */
  openQuality?: 'ok' | 'rejected' | null;
  openRejectReason?: string | null;
  reach: PoolMark;
  /** Realistic slot-delayed entries keyed slot1, slot2, … (null = unpriced). */
  delayed?: Record<string, PoolMark | null>;
  /** Trailing fill model per realistic leg (src/racer/fills.ts); absent = no `_adj` legs. */
  fill?: Record<string, LegFill | null>;
  /** Whether the base vault was armed before the migrate (see slotEntry.ts). */
  armed?: boolean | null;
  entryQuality?: EntryQuality;
  sameSlotDepletionPct?: number | null;
  /** Time already elapsed since the migrate when tracking starts; horizons stay anchored on the migrate. */
  elapsedMs?: number;
  entryFeatures?: EntryFeatureSnapshot | null;
}

/** Immutable data available when the migrate transaction reached this worker. */
export interface EntryFeatureSnapshot {
  version: 1;
  observedAtMs: number;
  migrateSlot: number | null;
  fireToCompleteSlots: number | null;
  completeToMigrateSlots: number | null;
  caller: string | null;
  quoteVaultSol: number | null;
  tokenProgram: string | null;
  sameSlotDepletionPct: number | null;
  knownFailedMigrateAttempts: number;
}

export interface PaperTrack {
  mint: string;
  poolAddress: string;
  entrySlot: number | null;
  sizeSol: number;
  walletBalanceSol: number | null;
  sizingReason: string | null;
  open: PoolMark | null;
  openQuality: 'ok' | 'rejected' | null;
  openRejectReason: string | null;
  reach: PoolMark;
  delayed: Record<string, PoolMark | null>;
  fill: Record<string, LegFill | null>;
  armed: boolean | null;
  entryQuality: EntryQuality;
  sameSlotDepletionPct: number | null;
  liveCostSol: number;
  /** 'measured' once the +1-slot buyers' fees + tips replaced the config default. */
  liveCostSource: 'config' | 'measured';
  /** Buy-side split when measured (fee route-independent, tip theirs); null with config costs. */
  liveCostBuyFeeSol: number | null;
  liveCostBuyTipSol: number | null;
  entryFeatures: EntryFeatureSnapshot | null;
  horizons: HorizonResult[];
}

/** 'gone' = the pool account no longer exists (read succeeded); null = unreadable right now. */
export type MarkRead = PoolMark | 'gone' | null;

export interface PaperDeps {
  /**
   * Stream-based horizon exit (decision mark + landed fill). Null = no
   * verified path for this mint, or a stream gap: fall back to readMark.
   */
  readPathExit?: (mint: string) => Promise<{ mid: MarkRead; exit: MarkRead; fill: string } | null>;
  readMark: (mint: string) => Promise<MarkRead>;
  /** Called at entry and after every horizon (upsert by mint). */
  sink: (t: PaperTrack) => void;
  schedule?: (ms: number, fn: () => void) => void;
}

export interface PaperOptions {
  horizonsSec?: number[];
  sizeSol?: number;
  maxPending?: number;
  /** Per-round-trip live execution cost charged to the realistic entries (SOL). */
  liveCostSol?: number;
  /** Realistic exits are marked this long after the horizon (sell send → land). */
  exitLandMs?: number;
  /** Slippage gate (roundTripGate): loosest live buy bound and worst ordinary exit tier. Undefined = that side ungated. */
  maxEntrySlippagePct?: number | undefined;
  maxExitSlippagePct?: number | undefined;
}

/**
 * Re-cost one horizon's realistic legs by a cost delta (in place): each
 * conditional leg shifts by the delta, and the expectancy leg follows scaled
 * by fill-p. Shared by live setLiveCost and the backfill CLI, so backfilled
 * tracks match tracks measured live by construction. Rugged horizons are the
 * caller's to skip; null legs and missing fill stay untouched.
 */
export function recostPnlForDelta(
  pnl: Record<string, number | null | undefined>,
  fill: Record<string, { p: number } | null | undefined>,
  names: readonly string[],
  deltaPct: number,
): void {
  for (const name of names) {
    const v = pnl[name];
    if (v !== null && v !== undefined) pnl[name] = v - deltaPct;
    const adj = pnl[`${name}_adj`];
    const f = fill[name];
    if (adj !== null && adj !== undefined && f) pnl[`${name}_adj`] = adj - deltaPct * f.p;
  }
}

export class PaperTracker {
  private readonly deps: PaperDeps;
  private readonly horizonsSec: number[];
  private readonly sizeSol: number;
  private readonly maxPending: number;
  private readonly liveCostSol: number;
  private readonly exitLandMs: number;
  private readonly maxEntrySlippagePct: number | undefined;
  private readonly maxExitSlippagePct: number | undefined;
  private readonly live = new Map<string, PaperTrack>();
  private readonly schedule: (ms: number, fn: () => void) => void;
  private pending = 0;
  abandoned = 0;

  constructor(deps: PaperDeps, opts: PaperOptions = {}) {
    this.deps = deps;
    this.horizonsSec = opts.horizonsSec ?? [30, 60, 300];
    this.sizeSol = opts.sizeSol ?? 0.1;
    this.maxPending = opts.maxPending ?? 50;
    this.liveCostSol = opts.liveCostSol ?? 0;
    this.exitLandMs = opts.exitLandMs ?? 0;
    this.maxEntrySlippagePct = opts.maxEntrySlippagePct;
    this.maxExitSlippagePct = opts.maxExitSlippagePct;
    this.schedule = deps.schedule ?? ((ms, fn) => void setTimeout(fn, ms));
  }

  get pendingCount(): number {
    return this.pending;
  }

  track(mint: string, entry: PaperEntry): boolean {
    if (this.pending >= this.maxPending) {
      this.abandoned++;
      return false;
    }
    const track: PaperTrack = {
      mint,
      poolAddress: entry.poolAddress,
      entrySlot: entry.slot,
      sizeSol: entry.sizeSol ?? this.sizeSol,
      walletBalanceSol: entry.walletBalanceSol ?? null,
      sizingReason: entry.sizingReason ?? null,
      open: entry.open,
      openQuality: entry.openQuality ?? null,
      openRejectReason: entry.openRejectReason ?? null,
      reach: entry.reach,
      delayed: entry.delayed ?? {},
      fill: entry.fill ?? {},
      armed: entry.armed ?? null,
      entryQuality: entry.entryQuality ?? 'missing-anchor',
      sameSlotDepletionPct: entry.sameSlotDepletionPct ?? null,
      liveCostSol: this.liveCostSol,
      liveCostSource: 'config',
      liveCostBuyFeeSol: null,
      liveCostBuyTipSol: null,
      entryFeatures: entry.entryFeatures ?? null,
      horizons: this.horizonsSec.map((sec) => ({ sec, price: null, quoteSol: null, pnlOpenPct: null, pnlReachPct: null, pnl: {} })),
    };
    this.deps.sink(track);
    if (track.horizons.length === 0) return true;
    this.pending++;
    this.live.set(mint, track);
    let remaining = track.horizons.length;
    for (const h of track.horizons) {
      this.schedule(Math.max(0, h.sec * 1000 - (entry.elapsedMs ?? 0)), () =>
        void this.readHorizon(track, h).finally(() => {
          this.deps.sink(track);
          if (--remaining === 0) {
            this.pending--;
            this.live.delete(mint);
          }
        }),
      );
    }
    return true;
  }

  /**
   * Replace the live cost with a measured one. Horizons already marked are
   * re-costed (the realistic legs are gross − cost), and the track re-sunk.
   * The buy-side split rides along for the fee/tip decomposition.
   */
  setLiveCost(mint: string, costSol: number, parts?: { buyFeeSol: number; buyTipSol: number }): void {
    const track = this.live.get(mint);
    if (!track || !(costSol >= 0)) return;
    const deltaPct = ((costSol - track.liveCostSol) / track.sizeSol) * 100;
    track.liveCostSol = costSol;
    track.liveCostSource = 'measured';
    if (parts) {
      track.liveCostBuyFeeSol = parts.buyFeeSol;
      track.liveCostBuyTipSol = parts.buyTipSol;
    }
    for (const h of track.horizons) {
      if (!h.rugged) recostPnlForDelta(h.pnl, track.fill, Object.keys(track.delayed), deltaPct);
    }
    this.deps.sink(track);
  }

  private async readHorizon(track: PaperTrack, h: HorizonResult): Promise<void> {
    // Preferred: stream states (no RPC; the sell fills in the next slot).
    const viaPath = this.deps.readPathExit ? await this.deps.readPathExit(track.mint).catch(() => null) : null;
    const mark: MarkRead = viaPath ? viaPath.mid : await this.deps.readMark(track.mint).catch(() => null);
    h.source = viaPath ? 'path' : 'rpc';
    if (viaPath) h.fill = viaPath.fill;
    if (mark === null) return;
    if (mark === 'gone') {
      this.rug(track, h);
      return;
    }
    h.price = mark.price;
    h.quoteSol = mark.quoteSol;
    h.pnlReachPct = this.scoreLeg(track, h, 'reach', track.reach, mark, 0);
    h.pnlOpenPct = track.open ? this.scoreLeg(track, h, 'open', track.open, mark, 0) : null;
    h.pnl = { open: h.pnlOpenPct, reach: h.pnlReachPct };
    // Realistic legs sell where the sell lands: the path's next-slot state, or
    // (fallback) an RPC read exitLandMs later.
    let exit: MarkRead = mark;
    if (viaPath) exit = viaPath.exit;
    else if (this.exitLandMs > 0) {
      await new Promise<void>((resolve) => this.schedule(this.exitLandMs, resolve));
      exit = await this.deps.readMark(track.mint).catch(() => null);
    }
    if (exit === null) {
      h.exitPrice = null;
      h.exitQuoteSol = null;
      h.exitRealQuoteSol = null;
      for (const name of Object.keys(track.delayed)) h.pnl[name] = null;
      return;
    }
    if (exit === 'gone') {
      this.rug(track, h);
      return;
    }
    h.exitAtMs = Date.now();
    h.exitPrice = exit.price;
    h.exitQuoteSol = exit.quoteSol;
    h.exitRealQuoteSol = exit.realQuoteSol ?? null;
    const costPct = (track.liveCostSol / track.sizeSol) * 100;
    for (const [name, entry] of Object.entries(track.delayed)) {
      const v = entry ? this.scoreLeg(track, h, name, entry, exit, costPct) : null;
      h.pnl[name] = v;
      const fill = track.fill[name];
      if (v !== null && fill) h.pnl[`${name}_adj`] = adjustLegForFill(v, fill, track.sizeSol);
    }
  }

  /**
   * One scored leg: the round trip at track size, gated by the live slippage
   * bounds. Gated legs score null with the reason on the horizon; unusable
   * marks score null without one. `extraCostPct` is the live-cost haircut for
   * realistic legs (idealised legs pay none).
   */
  private scoreLeg(
    track: PaperTrack,
    h: HorizonResult,
    name: string,
    entry: PoolMark,
    exit: PoolMark,
    extraCostPct: number,
  ): number | null {
    const parts = roundTripParts(entry, exit, track.sizeSol);
    const gate = roundTripGate(parts, this.maxEntrySlippagePct, this.maxExitSlippagePct);
    if (gate) {
      (h.gateReasons ??= {})[name] = gate;
      return null;
    }
    return parts === null ? null : parts.pct - extraCostPct;
  }

  private rug(track: PaperTrack, h: HorizonResult): void {
    h.exitAtMs = Date.now();
    h.rugged = true;
    h.price = 0;
    h.exitPrice = 0;
    h.exitQuoteSol = 0;
    h.exitRealQuoteSol = 0;
    h.pnlReachPct = -100;
    h.pnlOpenPct = track.open ? -100 : null;
    h.pnl = { open: h.pnlOpenPct, reach: -100 };
    const costPct = (track.liveCostSol / track.sizeSol) * 100;
    for (const [name, entry] of Object.entries(track.delayed)) {
      const v = entry ? -100 - costPct : null;
      h.pnl[name] = v;
      const fill = track.fill[name];
      if (v !== null && fill) h.pnl[`${name}_adj`] = adjustLegForFill(v, fill, track.sizeSol);
    }
  }
}
