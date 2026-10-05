/**
 * Landing distribution for the feasible leg (paper-honesty Unit 7).
 *
 * The feasible leg used to land at one modeled slot (feasibleSlot): the slot
 * in progress when this migration's own pipeline probe plus a one-way
 * allowance would reach the leader. That point estimate ignores variance —
 * sometimes the pipeline is slow and we miss slot+1. The honest version
 * pushes the TRAILING pipeline samples (send_ready_us over the landing
 * lookback) through the slot boundaries and lands the leg on the resulting
 * offset distribution instead.
 *
 * The distributed feasible leg is an expectancy over offsets 1..3 plus a
 * tail (landed beyond +3, or never): window mass prices the slot legs, the
 * tail and gate-impossible offsets pay attempt costs without a fill. The base
 * leg renormalizes over priced offsets (conditional on landing somewhere
 * priced); the expectancy leg fails open to null unless every unit of mass
 * is accounted — a partial expectancy would masquerade as complete.
 */
import type { LegFill } from './fills.ts';

/** P(our send lands at migrate+1/+2/+3); tail = beyond +3. Sums to 1. */
export interface LandingWeights {
  w1: number;
  w2: number;
  w3: number;
  tail: number;
  /** Tally inputs (mappable trailing samples). */
  n: number;
}

/** Tally landing offsets (slot − migrateSlot) into weights. Null = no mappable samples. */
export function tallyLandingOffsets(offsets: ReadonlyArray<number | null>): LandingWeights | null {
  let c1 = 0;
  let c2 = 0;
  let c3 = 0;
  let tail = 0;
  for (const o of offsets) {
    if (o === null || o === undefined || !Number.isFinite(o)) continue;
    // Ready before the migrate settled still lands no earlier than +1 (same
    // floor as the point feasibleSlot).
    if (o <= 1) c1++;
    else if (o <= 2) c2++;
    else if (o <= 3) c3++;
    else tail++;
  }
  const n = c1 + c2 + c3 + tail;
  if (n === 0) return null;
  return { w1: c1 / n, w2: c2 / n, w3: c3 / n, tail: tail / n, n };
}

/**
 * Blended fill for the distributed feasible leg. The p (window E[p]) is
 * load-bearing: it scales the expectancy leg under live-cost re-costing
 * exactly, because gated/tail cost terms carry no live cost. costSol blends
 * window attempt costs plus the tail's (charged at the offset-3 rate —
 * attempts burn through the window); informational only, since the
 * distributed expectancy is computed directly, never via adjustLegForFill.
 */
export function blendFillForLanding(
  fills: { slot1: LegFill | null; slot2: LegFill | null; slot3: LegFill | null },
  weights: LandingWeights,
): LegFill | null {
  const ws = [weights.w1, weights.w2, weights.w3];
  const fs = [fills.slot1, fills.slot2, fills.slot3];
  let p = 0;
  let costSol = 0;
  let pMass = 0;
  let exitFailCostSol: number | undefined;
  fs.forEach((f, i) => {
    if (!f) return;
    p += ws[i]! * f.p;
    costSol += ws[i]! * f.costSol;
    pMass += ws[i]!;
    exitFailCostSol ??= f.exitFailCostSol;
  });
  if (pMass === 0) return null;
  if (weights.tail > 0 && fills.slot3) costSol += weights.tail * fills.slot3.costSol;
  const out: LegFill = { p, costSol };
  if (exitFailCostSol !== undefined) out.exitFailCostSol = exitFailCostSol;
  return out;
}

export interface DistributedLegsInput {
  /** Priced legs incl. expectancy legs (h.pnl); components read as slot{k}{suffix}. */
  legs: Readonly<Record<string, number | null | undefined>>;
  gateReasons: Readonly<Record<string, string>> | undefined;
  fills: Readonly<Record<string, LegFill | null | undefined>>;
  sizeSol: number;
  weights: LandingWeights;
  /** '' prices feasible from slot{k}; 'mid' prices feasiblemid from slot{k}mid. */
  suffix: '' | 'mid';
}

export function scoreDistributedLegs(input: DistributedLegsInput): { value: number | null; adj: number | null } {
  const { legs, gateReasons, fills, sizeSol, weights, suffix } = input;
  const w = [weights.w1, weights.w2, weights.w3];
  // Attempt cost at an offset: its own fill entry, else the offset-3 rate.
  const costPctFor = (k: number): number | null => {
    const f = fills[`slot${k}${suffix}`] ?? fills[`slot3${suffix}`];
    if (!f || !(sizeSol > 0)) return null;
    return ((f.costSol + (f.exitFailCostSol ?? 0)) / sizeSol) * 100;
  };
  let num = 0;
  let den = 0;
  let adjNum = 0;
  let adjDen = 0;
  for (let k = 1; k <= 3; k++) {
    const name = `slot${k}${suffix}`;
    const v = legs[name];
    if (v !== null && v !== undefined) {
      num += w[k - 1]! * v;
      den += w[k - 1]!;
    }
    const a = legs[`${name}_adj`];
    if (a !== null && a !== undefined) {
      adjNum += w[k - 1]! * a;
      adjDen += w[k - 1]!;
    } else if (gateReasons?.[name]) {
      // Landed at an impossible offset: attempts paid, no fill.
      const c = costPctFor(k);
      if (c !== null) {
        adjNum += w[k - 1]! * -c;
        adjDen += w[k - 1]!;
      }
    }
    // Data-gap nulls stay out of both legs (base renormalizes; adj fails open).
  }
  if (weights.tail > 0) {
    const c = costPctFor(3);
    if (c !== null) {
      adjNum += weights.tail * -c;
      adjDen += weights.tail;
    }
  }
  return {
    value: den > 0 ? num / den : null,
    adj: adjDen >= 1 - 1e-9 ? adjNum : null,
  };
}
