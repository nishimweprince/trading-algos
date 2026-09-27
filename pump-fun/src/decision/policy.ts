import type { Config } from '../config/schema.ts';
import type { Answer, DecisionResult } from './types.ts';

/**
 * Code applies policy (the model only answers questions). One threshold per
 * question, sized to what being wrong costs; never the paper's 2p-1 Kelly —
 * that assumes an even-money payoff, and a momentum scalp's payoff is not.
 * The gate can only veto or size an accept; it never rescues a veto.
 */
export interface GateOutcome {
  /** Null = passes the gate. */
  veto: string | null;
  /** Multiplier for computeEntrySizeSol (1 unless gate.sizeByProb). */
  sizeFactor: number;
  /** Continuation probability after recalibration, for the record. */
  continuation: number | null;
}

type GateCfg = Config['decision']['gate'];

/** Platt recalibration p' = sigmoid(a·logit(p) + b); identity when not fitted. */
export function recalibrate(p: number, cal: { a: number; b: number } | undefined): number {
  if (!cal) return p;
  const q = Math.min(1 - 1e-6, Math.max(1e-6, p));
  const z = cal.a * Math.log(q / (1 - q)) + cal.b;
  return 1 / (1 + Math.exp(-z));
}

function numeric(a: Answer | undefined): number | null {
  return a && typeof a.value === 'number' && Number.isFinite(a.value) ? a.value : null;
}

export function evaluateGate(result: Pick<DecisionResult, 'answers'>, gate: GateCfg): GateOutcome {
  const by = new Map(result.answers.map((a) => [a.id, a]));
  const prob = (id: string) => {
    const v = numeric(by.get(id));
    return v === null ? null : recalibrate(v, gate.calibration[id]);
  };
  const continuation = prob('continuation');

  // A configured question the model did not answer fails closed: the gate was
  // asked to hold that line and cannot vouch for it.
  for (const [id, floor] of Object.entries(gate.minProb)) {
    const p = prob(id);
    if (p === null || p < floor) return { veto: `JEV_SKIP:${id}`, sizeFactor: 0, continuation };
  }
  for (const [id, ceiling] of Object.entries(gate.maxProb)) {
    const p = prob(id);
    if (p === null || p > ceiling) return { veto: `JEV_SKIP:${id}`, sizeFactor: 0, continuation };
  }
  for (const [id, floor] of Object.entries(gate.minScore)) {
    const s = numeric(by.get(id));
    if (s === null || s < floor) return { veto: `JEV_SKIP:${id}`, sizeFactor: 0, continuation };
  }

  let sizeFactor = 1;
  const floor = gate.minProb.continuation;
  if (gate.sizeByProb && continuation !== null && floor) {
    sizeFactor = Math.min(1.25, Math.max(0.5, continuation / floor));
  }
  return { veto: null, sizeFactor, continuation };
}
