import type { Config } from '../config/schema.ts';
import { barriersFor, type PathTick } from '../research/labels.ts';

/**
 * Volatility-scaled exits at runtime (work plan 2026-09-25 P3.5). Collects
 * the first `vol.lookbackMs` of a position's ticks, then yields TP / stop
 * levels once — computed by the SAME `barriersFor` the exit-grid research
 * and the triple-barrier labels use, so what is tuned offline is exactly what
 * runs. Shared by the manager and the dry-run twin (identical rules).
 */
export class AdaptiveExit {
  private readonly cfg: Config['exits'];
  private readonly openedAtMs: number;
  private readonly path: PathTick[] = [];
  private done = false;

  constructor(cfg: Config['exits'], openedAtMs: number, entryPrice: number) {
    this.cfg = cfg;
    this.openedAtMs = openedAtMs;
    this.path.push({ tMs: 0, price: entryPrice });
    this.done = cfg.mode !== 'volatility';
  }

  /** Feed a usable tick; returns new barriers exactly once, when the lookback completes. */
  observe(price: number, atMs: number): { tpPct: number; slPct: number; sigmaPct: number | null } | null {
    if (this.done) return null;
    const t = atMs - this.openedAtMs;
    this.path.push({ tMs: t, price });
    if (t < this.cfg.vol.lookbackMs) return null;
    this.done = true;
    const v = this.cfg.vol;
    return barriersFor(
      {
        mode: 'volatility',
        tpPct: this.cfg.tp1Pct,
        slPct: this.cfg.hardStopPct,
        k1: v.k1,
        k2: v.k2,
        minTpPct: v.minTpPct,
        maxTpPct: v.maxTpPct,
        minSlPct: v.minSlPct,
        maxSlPct: v.maxSlPct,
        volLookbackMs: v.lookbackMs,
        timeStopMs: this.cfg.timeStopMinutes * 60_000,
      },
      this.path,
    );
  }
}
