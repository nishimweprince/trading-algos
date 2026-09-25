/**
 * Triple-barrier labels on recorded tick paths through the honest simulator
 * (work plan 2026-09-25 P3.4 / P3.5; López de Prado ch. 3 [R6]).
 *
 * For an entry at the first tick of a path:
 *   upper barrier  +tpPct   -> exit fills at the price `exitLatencyMs` later
 *   lower barrier  −slPct   -> exit fills at the WORST price within the latency
 *   vertical       timeStop -> exit fills at the price `exitLatencyMs` later
 * Barriers may be volatility-scaled: tp = k1·σ, sl = k2·σ, clipped, with σ the
 * realized volatility of the first `volLookbackMs` of the path.
 * Net return subtracts the real PumpSwap tier fee on each leg and fixed tx
 * costs, so a label is "profitable AFTER costs", never a gross coin flip.
 */
import { feeBpsForMcap, mcapFromPrice } from '../positions/feeTiers.ts';

export interface PathTick {
  tMs: number;
  price: number;
}

export interface BarrierSpec {
  mode: 'fixed' | 'volatility';
  tpPct: number;
  slPct: number;
  k1?: number;
  k2?: number;
  minTpPct?: number;
  maxTpPct?: number;
  minSlPct?: number;
  maxSlPct?: number;
  volLookbackMs?: number;
  timeStopMs: number;
}

export interface CostSpec {
  exitLatencyMs: number;
  /** Position size (SOL) for the fixed tx-cost share. */
  sizeSol: number;
  txCostSol: number;
  /** Entry is delayed by this much after the path's first tick (entry latency). */
  entryLatencyMs?: number;
}

export interface LabelResult {
  label: 0 | 1;
  netReturn: number;
  grossReturn: number;
  barrier: 'upper' | 'lower' | 'vertical' | 'end';
  holdMs: number;
  tpPct: number;
  slPct: number;
  sigmaPct: number | null;
}

/** Realized volatility (%) of log returns over the first `lookbackMs`. */
export function realizedVolPct(path: readonly PathTick[], lookbackMs: number): number | null {
  const pts = path.filter((p) => p.tMs <= (path[0]?.tMs ?? 0) + lookbackMs && p.price > 0);
  if (pts.length < 3) return null;
  let ss = 0;
  for (let i = 1; i < pts.length; i++) ss += Math.log(pts[i]!.price / pts[i - 1]!.price) ** 2;
  return Math.sqrt(ss) * 100;
}

export function barriersFor(spec: BarrierSpec, path: readonly PathTick[]): { tpPct: number; slPct: number; sigmaPct: number | null } {
  if (spec.mode === 'fixed') return { tpPct: spec.tpPct, slPct: spec.slPct, sigmaPct: null };
  const sigma = realizedVolPct(path, spec.volLookbackMs ?? 3_000);
  if (sigma === null) return { tpPct: spec.tpPct, slPct: spec.slPct, sigmaPct: null };
  const clip = (v: number, lo = 0, hi = Infinity) => Math.min(hi, Math.max(lo, v));
  return {
    tpPct: clip((spec.k1 ?? 2) * sigma, spec.minTpPct ?? 0, spec.maxTpPct ?? Infinity),
    slPct: clip((spec.k2 ?? 2) * sigma, spec.minSlPct ?? 0, spec.maxSlPct ?? Infinity),
    sigmaPct: sigma,
  };
}

function priceAt(path: readonly PathTick[], tMs: number): number {
  let p = path[0]!.price;
  for (const x of path) {
    if (x.tMs > tMs) break;
    p = x.price;
  }
  return p;
}

function worstIn(path: readonly PathTick[], from: number, to: number, start: number): number {
  let w = start;
  for (const x of path) if (x.tMs >= from && x.tMs <= to && x.price < w) w = x.price;
  return w;
}

export function tripleBarrier(path: readonly PathTick[], spec: BarrierSpec, cost: CostSpec): LabelResult | null {
  const clean = path.filter((p) => p.price > 0 && Number.isFinite(p.price));
  if (clean.length < 2) return null;
  const t0 = clean[0]!.tMs + (cost.entryLatencyMs ?? 0);
  const entry = priceAt(clean, t0);
  const { tpPct, slPct, sigmaPct } = barriersFor(spec, clean);
  const up = entry * (1 + tpPct / 100);
  const down = entry * (1 - slPct / 100);
  let exitPrice = clean[clean.length - 1]!.price;
  let barrier: LabelResult['barrier'] = 'end';
  let exitT = clean[clean.length - 1]!.tMs;
  for (const x of clean) {
    if (x.tMs < t0) continue;
    if (x.price <= down) {
      exitT = x.tMs + cost.exitLatencyMs;
      exitPrice = worstIn(clean, x.tMs, exitT, x.price);
      barrier = 'lower';
      break;
    }
    if (x.price >= up) {
      exitT = x.tMs + cost.exitLatencyMs;
      exitPrice = priceAt(clean, exitT);
      barrier = 'upper';
      break;
    }
    if (x.tMs - t0 >= spec.timeStopMs) {
      exitT = x.tMs + cost.exitLatencyMs;
      exitPrice = priceAt(clean, exitT);
      barrier = 'vertical';
      break;
    }
  }
  const gross = exitPrice / entry - 1;
  const entryFee = feeBpsForMcap(mcapFromPrice(entry)) / 10_000;
  const exitFee = (feeBpsForMcap(mcapFromPrice(exitPrice)) / 10_000) * (1 + gross);
  const tx = cost.sizeSol > 0 ? (2 * cost.txCostSol) / cost.sizeSol : 0;
  const net = gross - entryFee - exitFee - tx;
  return { label: net > 0 ? 1 : 0, netReturn: net, grossReturn: gross, barrier, holdMs: exitT - t0, tpPct, slPct, sigmaPct };
}
