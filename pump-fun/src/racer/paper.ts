/**
 * First-position paper tracker. On an observed migration, prices the new pool
 * as fast as possible (paper entry) and re-prices at fixed horizons, answering
 * the verdict's open question — does first position pay — with zero sends.
 * Fixed-horizon exits only; no exit FSM (the dry-run twin already owns that).
 */
import { paperPnlPct } from './economics.ts';

export interface PoolPrice {
  price: number;
  poolAddress: string;
  slot: number | null;
}

export interface HorizonResult {
  sec: number;
  price: number | null;
  pnlPct: number | null;
}

export interface PaperTrack {
  mint: string;
  poolAddress: string;
  entryPrice: number;
  entrySlot: number | null;
  sizeSol: number;
  horizons: HorizonResult[];
}

export interface PaperDeps {
  readPrice: (mint: string) => Promise<PoolPrice | null>;
  sink: (t: PaperTrack) => void;
  schedule?: (ms: number, fn: () => void) => void;
}

export interface PaperOptions {
  horizonsSec?: number[];
  sizeSol?: number;
  /** Round-trip fee drag applied to paper PnL. */
  feeFraction?: number;
  entryRetries?: number;
  entryRetryMs?: number;
  maxPending?: number;
}

export class PaperTracker {
  private readonly deps: PaperDeps;
  private readonly horizonsSec: number[];
  private readonly sizeSol: number;
  private readonly feeFraction: number;
  private readonly entryRetries: number;
  private readonly entryRetryMs: number;
  private readonly maxPending: number;
  private readonly schedule: (ms: number, fn: () => void) => void;
  private pending = 0;
  abandoned = 0;

  constructor(deps: PaperDeps, opts: PaperOptions = {}) {
    this.deps = deps;
    this.horizonsSec = opts.horizonsSec ?? [30, 60, 300];
    this.sizeSol = opts.sizeSol ?? 0.1;
    this.feeFraction = opts.feeFraction ?? 0.01;
    this.entryRetries = opts.entryRetries ?? 5;
    this.entryRetryMs = opts.entryRetryMs ?? 2000;
    this.maxPending = opts.maxPending ?? 50;
    this.schedule = deps.schedule ?? ((ms, fn) => void setTimeout(fn, ms));
  }

  get pendingCount(): number {
    return this.pending;
  }

  /** Start a paper track. Entry is priced immediately with retries; horizons fan out from entry. */
  track(mint: string): void {
    if (this.pending >= this.maxPending) {
      this.abandoned++;
      return;
    }
    this.pending++;
    void this.enter(mint, 0);
  }

  private async enter(mint: string, attempt: number): Promise<void> {
    const entry = await this.deps.readPrice(mint).catch(() => null);
    if (!entry) {
      if (attempt + 1 >= this.entryRetries) {
        this.pending--;
        this.abandoned++;
        return;
      }
      this.schedule(this.entryRetryMs, () => void this.enter(mint, attempt + 1));
      return;
    }
    const track: PaperTrack = {
      mint,
      poolAddress: entry.poolAddress,
      entryPrice: entry.price,
      entrySlot: entry.slot,
      sizeSol: this.sizeSol,
      horizons: this.horizonsSec.map((sec) => ({ sec, price: null, pnlPct: null })),
    };
    if (this.horizonsSec.length === 0) {
      this.pending--;
      this.deps.sink(track);
      return;
    }
    let remaining = this.horizonsSec.length;
    for (const h of track.horizons) {
      this.schedule(h.sec * 1000, () => void this.readHorizon(track, h, () => {
        if (--remaining === 0) {
          this.pending--;
          this.deps.sink(track);
        }
      }));
    }
  }

  private async readHorizon(track: PaperTrack, h: HorizonResult, done: () => void): Promise<void> {
    const p = await this.deps.readPrice(track.mint).catch(() => null);
    if (p) {
      h.price = p.price;
      h.pnlPct = paperPnlPct(track.entryPrice, p.price, this.feeFraction);
    }
    done();
  }
}
