import { bootstrapCI, type Interval } from '../research/stats.ts';

/**
 * Rolling edge monitor (work plan 2026-09-25 P4.3). Keeps the net return
 * (% of size) of the last `window` closed trades and answers whether the
 * bootstrap CI of their mean lies entirely below zero — i.e. the data rule out
 * a non-negative edge at `level`. The RiskManager turns that into the
 * NEGATIVE_EDGE breaker; the dashboard shows the same interval.
 *
 * Deterministic for a given seed and window contents, so the breaker cannot
 * flap on resampling noise between two reconciles over the same trades.
 */
export interface EdgeMonitorOpts {
  window: number;
  minTrades: number;
  level: number;
  iterations: number;
  seed: number;
}

export interface EdgeState {
  n: number;
  ci: Interval | null;
  negative: boolean;
}

export class EdgeMonitor {
  private readonly opts: EdgeMonitorOpts;
  /** Oldest first. */
  private returnsPct: number[] = [];
  private cached: EdgeState | null = null;

  constructor(opts: EdgeMonitorOpts) {
    this.opts = opts;
  }

  /** Replace the window (rehydration). `newestFirst` as the repository returns it. */
  seed(newestFirst: readonly number[]): void {
    this.returnsPct = newestFirst.slice(0, this.opts.window).reverse();
    this.cached = null;
  }

  record(returnPct: number): void {
    if (!Number.isFinite(returnPct)) return;
    this.returnsPct.push(returnPct);
    if (this.returnsPct.length > this.opts.window) this.returnsPct.shift();
    this.cached = null;
  }

  state(): EdgeState {
    if (this.cached) return this.cached;
    const n = this.returnsPct.length;
    if (n < this.opts.minTrades) {
      this.cached = { n, ci: null, negative: false };
      return this.cached;
    }
    const ci = bootstrapCI(this.returnsPct, { seed: this.opts.seed, iterations: this.opts.iterations, level: this.opts.level });
    this.cached = { n, ci, negative: ci.hi < 0 };
    return this.cached;
  }
}
