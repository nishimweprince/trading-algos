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
 *  - A pool that is gone (account closed) scores −100% for every entry: a rug
 *    is a total loss, never a dropped sample (dropping it biased medians up).
 *    A sell is also capped by the SOL really in the quote vault.
 *  - Realistic legs exit where a sell would LAND: the pool `exitLandMs` after
 *    the horizon (send → land ≈ 1 slot, measured live), not the horizon mid.
 *    open / reach keep the horizon mid (they are idealised by definition).
 *  - Realistic legs pay `liveCostSol`, replaced by the measured cost of that
 *    migration's +1-slot buyers when it arrives (setLiveCost).
 */
import { paperRoundTripPct, type PoolMark } from './economics.ts';
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
  reach: PoolMark;
  /** Realistic slot-delayed entries keyed slot1, slot2, … (null = unpriced). */
  delayed?: Record<string, PoolMark | null>;
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
  reach: PoolMark;
  delayed: Record<string, PoolMark | null>;
  armed: boolean | null;
  entryQuality: EntryQuality;
  sameSlotDepletionPct: number | null;
  liveCostSol: number;
  /** 'measured' once the +1-slot buyers' fees + tips replaced the config default. */
  liveCostSource: 'config' | 'measured';
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
}

export class PaperTracker {
  private readonly deps: PaperDeps;
  private readonly horizonsSec: number[];
  private readonly sizeSol: number;
  private readonly maxPending: number;
  private readonly liveCostSol: number;
  private readonly exitLandMs: number;
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
      reach: entry.reach,
      delayed: entry.delayed ?? {},
      armed: entry.armed ?? null,
      entryQuality: entry.entryQuality ?? 'missing-anchor',
      sameSlotDepletionPct: entry.sameSlotDepletionPct ?? null,
      liveCostSol: this.liveCostSol,
      liveCostSource: 'config',
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
   */
  setLiveCost(mint: string, costSol: number): void {
    const track = this.live.get(mint);
    if (!track || !(costSol >= 0)) return;
    const deltaPct = ((costSol - track.liveCostSol) / track.sizeSol) * 100;
    track.liveCostSol = costSol;
    track.liveCostSource = 'measured';
    for (const h of track.horizons) {
      for (const name of Object.keys(track.delayed)) {
        const v = h.pnl[name];
        if (v !== null && v !== undefined && !h.rugged) h.pnl[name] = v - deltaPct;
      }
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
    h.pnlReachPct = paperRoundTripPct(track.reach, mark, track.sizeSol);
    h.pnlOpenPct = track.open ? paperRoundTripPct(track.open, mark, track.sizeSol) : null;
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
      const gross = entry ? paperRoundTripPct(entry, exit, track.sizeSol) : null;
      h.pnl[name] = gross === null ? null : gross - costPct;
    }
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
    for (const [name, entry] of Object.entries(track.delayed)) h.pnl[name] = entry ? -100 - costPct : null;
  }
}
