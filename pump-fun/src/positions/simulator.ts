import type { ExitTrigger } from '../core/types.ts';
import { logger } from '../core/logger.ts';
import { mulberry32 } from '../research/stats.ts';

/**
 * Honest simulator primitives (work plan 2026-09-25 P1.2 / P1.3, F5 / F6).
 *
 * Paper, dry-run twin and shadow fills used to happen at the exact tick that
 * triggered them, with no confirm latency, no failure and no adverse fill —
 * while live averaged 1.3 s detect->open, 1.26 s exit confirm and failed
 * 15/33 entries. These pieces make paper pessimistic enough that live can
 * only match or beat it:
 *
 *  - LatencyModel: empirical latency_samples once there are enough, else a
 *    lognormal pinned to recorded (median, p90).
 *  - PendingExit: an exit fires at the trigger tick but FILLS after the
 *    sampled confirm latency — at the WORST price seen in that window for
 *    protective exits (stop / trailing / emergency), at the price when the
 *    confirm lands for the rest.
 *  - entry haircut + failure draws.
 *
 * All draws come from one seeded PRNG, so a replay with the same seed and the
 * same tick stream reproduces fills exactly.
 */

export type LatencyKindSim = 'entry_confirm' | 'exit_confirm';

export interface SimulatorCfg {
  enabled: boolean;
  seed: number;
  minSamples: number;
  entryConfirmMedianMs: number;
  entryConfirmP90Ms: number;
  exitConfirmMedianMs: number;
  exitConfirmP90Ms: number;
  entryHaircutPct: { min: number; mode: number; max: number };
  baseEntryFailPct: number;
  useExecutorSimulation?: boolean;
}

const Z90 = 1.2815515655446004;

/** Box–Muller standard normal from a uniform source. */
function normal(rng: () => number): number {
  const u = Math.max(rng(), 1e-12);
  const v = rng();
  return Math.sqrt(-2 * Math.log(u)) * Math.cos(2 * Math.PI * v);
}

/** Lognormal draw with the given median and 90th percentile. */
export function lognormalFromMedianP90(rng: () => number, medianMs: number, p90Ms: number): number {
  const mu = Math.log(Math.max(1, medianMs));
  const sigma = Math.max(0, Math.log(Math.max(p90Ms, medianMs) / Math.max(1, medianMs)) / Z90);
  return Math.exp(mu + sigma * normal(rng));
}

/** Triangular(min, mode, max) draw. */
export function triangular(rng: () => number, min: number, mode: number, max: number): number {
  if (!(max > min)) return min;
  const m = Math.min(max, Math.max(min, mode));
  const u = rng();
  const c = (m - min) / (max - min);
  return u < c ? min + Math.sqrt(u * (max - min) * (m - min)) : max - Math.sqrt((1 - u) * (max - min) * (max - m));
}

export class Simulator {
  readonly cfg: SimulatorCfg;
  private readonly rng: () => number;
  private readonly samples = new Map<LatencyKindSim, number[]>();
  private readonly log = logger.child({ mod: 'simulator' });

  constructor(cfg: SimulatorCfg, rng?: () => number) {
    this.cfg = cfg;
    this.rng = rng ?? mulberry32(cfg.seed);
  }

  get enabled(): boolean {
    return this.cfg.enabled;
  }

  /**
   * Replace the empirical pool for a latency kind (live samples only — the
   * simulator never writes its own draws into latency_samples, so it cannot
   * feed on itself).
   */
  setSamples(kind: LatencyKindSim, ms: readonly number[]): void {
    const clean = ms.filter((x) => Number.isFinite(x) && x > 0);
    this.samples.set(kind, clean);
    this.log.debug('latency samples loaded', { kind, n: clean.length, empirical: clean.length >= this.cfg.minSamples });
  }

  /** 'empirical' when the pool is large enough, else 'lognormal'. */
  latencySource(kind: LatencyKindSim): 'empirical' | 'lognormal' {
    return (this.samples.get(kind)?.length ?? 0) >= this.cfg.minSamples ? 'empirical' : 'lognormal';
  }

  sampleLatencyMs(kind: LatencyKindSim): number {
    const pool = this.samples.get(kind);
    if (pool && pool.length >= this.cfg.minSamples) return pool[Math.floor(this.rng() * pool.length)]!;
    return kind === 'entry_confirm'
      ? lognormalFromMedianP90(this.rng, this.cfg.entryConfirmMedianMs, this.cfg.entryConfirmP90Ms)
      : lognormalFromMedianP90(this.rng, this.cfg.exitConfirmMedianMs, this.cfg.exitConfirmP90Ms);
  }

  /** Adverse entry fill, % of price (always >= 0). */
  sampleEntryHaircutPct(): number {
    const h = this.cfg.entryHaircutPct;
    return Math.max(0, triangular(this.rng, h.min, h.mode, h.max));
  }

  /**
   * Did the simulated entry land? Mechanistic first: a mid move past the
   * widest buy slippage bound between screening and the landed fill is the
   * 6004 ExceededSlippage path that failed most live entries. Then a residual
   * random failure rate.
   */
  entryOutcome(movePct: number | null, maxSlippagePct: number): { ok: true } | { ok: false; reason: 'slippage_exceeded' | 'random_failure'; detail: string } {
    if (movePct !== null && movePct > maxSlippagePct) {
      return { ok: false, reason: 'slippage_exceeded', detail: `mid moved ${movePct.toFixed(2)}% > ${maxSlippagePct}% bound during confirm` };
    }
    if (this.rng() * 100 < this.cfg.baseEntryFailPct) {
      return { ok: false, reason: 'random_failure', detail: `residual failure draw (${this.cfg.baseEntryFailPct}%)` };
    }
    return { ok: true };
  }
}

/** Protective exits fill at the worst price in the confirm window. */
export function fillsAtWorst(trigger: ExitTrigger): boolean {
  return (
    trigger === 'STOP_LOSS' ||
    trigger === 'TRAILING_STOP' ||
    trigger === 'EMERGENCY_EXIT' ||
    trigger === 'NO_PRICE_DATA' ||
    trigger === 'KILL_SWITCH'
  );
}

/**
 * An exit that has triggered but not yet "confirmed". Feed it every usable
 * tick; it reports due once a tick at/after triggerAt + latency arrives (or
 * the caller's timer fires) and then yields the fill price.
 */
export class PendingExit<F extends { trigger: ExitTrigger; price: number }> {
  readonly fill: F;
  readonly triggerAtMs: number;
  readonly latencyMs: number;
  readonly dueAtMs: number;
  private worst: number;
  private atDue: number;

  constructor(fill: F, triggerAtMs: number, latencyMs: number) {
    this.fill = fill;
    this.triggerAtMs = triggerAtMs;
    this.latencyMs = latencyMs;
    this.dueAtMs = triggerAtMs + latencyMs;
    this.worst = fill.price;
    this.atDue = fill.price;
  }

  /**
   * Observe a price. Ticks inside the window update the worst and the
   * latest-before-due price; returns true when this tick is at/after due.
   * A tick past due is NOT folded in — the confirm landed before it.
   */
  observe(price: number, atMs: number): boolean {
    if (!(price > 0) || !Number.isFinite(price)) return atMs >= this.dueAtMs;
    if (atMs <= this.dueAtMs) {
      if (price < this.worst) this.worst = price;
      this.atDue = price;
      return atMs === this.dueAtMs;
    }
    return true;
  }

  isDue(nowMs: number): boolean {
    return nowMs >= this.dueAtMs;
  }

  /** Fill price once due. */
  settlePrice(): number {
    return fillsAtWorst(this.fill.trigger) ? this.worst : this.atDue;
  }
}
