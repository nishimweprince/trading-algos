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
 * the entry and exit depth. Zero sends; fixed-horizon exits only.
 */
import { paperRoundTripPct, type PoolMark } from './economics.ts';

export interface HorizonResult {
  sec: number;
  price: number | null;
  quoteSol: number | null;
  pnlOpenPct: number | null;
  pnlReachPct: number | null;
  /** Round trip % per entry name (open, reach, slot1, …); realistic legs net of liveCostSol. */
  pnl: Record<string, number | null>;
}

export interface PaperEntry {
  poolAddress: string;
  slot: number | null;
  open: PoolMark | null;
  reach: PoolMark;
  /** Realistic slot-delayed entries keyed slot1, slot2, … (null = unpriced). */
  delayed?: Record<string, PoolMark | null>;
  /** Whether the base vault was armed before the migrate (see slotEntry.ts). */
  armed?: boolean | null;
  /** Time already elapsed since the migrate when tracking starts; horizons stay anchored on the migrate. */
  elapsedMs?: number;
}

export interface PaperTrack {
  mint: string;
  poolAddress: string;
  entrySlot: number | null;
  sizeSol: number;
  open: PoolMark | null;
  reach: PoolMark;
  delayed: Record<string, PoolMark | null>;
  armed: boolean | null;
  liveCostSol: number;
  horizons: HorizonResult[];
}

export interface PaperDeps {
  readMark: (mint: string) => Promise<PoolMark | null>;
  sink: (t: PaperTrack) => void;
  schedule?: (ms: number, fn: () => void) => void;
}

export interface PaperOptions {
  horizonsSec?: number[];
  sizeSol?: number;
  maxPending?: number;
  /** Per-round-trip live execution cost charged to the realistic entries (SOL). */
  liveCostSol?: number;
}

export class PaperTracker {
  private readonly deps: PaperDeps;
  private readonly horizonsSec: number[];
  private readonly sizeSol: number;
  private readonly maxPending: number;
  private readonly liveCostSol: number;
  private readonly schedule: (ms: number, fn: () => void) => void;
  private pending = 0;
  abandoned = 0;

  constructor(deps: PaperDeps, opts: PaperOptions = {}) {
    this.deps = deps;
    this.horizonsSec = opts.horizonsSec ?? [30, 60, 300];
    this.sizeSol = opts.sizeSol ?? 0.1;
    this.maxPending = opts.maxPending ?? 50;
    this.liveCostSol = opts.liveCostSol ?? 0;
    this.schedule = deps.schedule ?? ((ms, fn) => void setTimeout(fn, ms));
  }

  get pendingCount(): number {
    return this.pending;
  }

  track(mint: string, entry: PaperEntry): void {
    if (this.pending >= this.maxPending) {
      this.abandoned++;
      return;
    }
    const track: PaperTrack = {
      mint,
      poolAddress: entry.poolAddress,
      entrySlot: entry.slot,
      sizeSol: this.sizeSol,
      open: entry.open,
      reach: entry.reach,
      delayed: entry.delayed ?? {},
      armed: entry.armed ?? null,
      liveCostSol: this.liveCostSol,
      horizons: this.horizonsSec.map((sec) => ({ sec, price: null, quoteSol: null, pnlOpenPct: null, pnlReachPct: null, pnl: {} })),
    };
    if (track.horizons.length === 0) {
      this.deps.sink(track);
      return;
    }
    this.pending++;
    let remaining = track.horizons.length;
    for (const h of track.horizons) {
      this.schedule(Math.max(0, h.sec * 1000 - (entry.elapsedMs ?? 0)), () =>
        void this.readHorizon(track, h).finally(() => {
          if (--remaining === 0) {
            this.pending--;
            this.deps.sink(track);
          }
        }),
      );
    }
  }

  private async readHorizon(track: PaperTrack, h: HorizonResult): Promise<void> {
    const mark = await this.deps.readMark(track.mint).catch(() => null);
    if (!mark) return;
    h.price = mark.price;
    h.quoteSol = mark.quoteSol;
    h.pnlReachPct = paperRoundTripPct(track.reach, mark, track.sizeSol);
    h.pnlOpenPct = track.open ? paperRoundTripPct(track.open, mark, track.sizeSol) : null;
    h.pnl = { open: h.pnlOpenPct, reach: h.pnlReachPct };
    const costPct = (track.liveCostSol / track.sizeSol) * 100;
    for (const [name, entry] of Object.entries(track.delayed)) {
      const gross = entry ? paperRoundTripPct(entry, mark, track.sizeSol) : null;
      h.pnl[name] = gross === null ? null : gross - costPct;
    }
  }
}
