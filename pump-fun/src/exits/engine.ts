import type { ExitTrigger } from '../core/types.ts';
import type { Config, ExitOverrides } from '../config/schema.ts';

/**
 * Exit-trigger evaluation (Section 7.3), as a pure function of a position's
 * state and the current price. Kept side-effect-free so it is exhaustively
 * testable; the PaperPosition (and later the live exit engine) applies the
 * decision. Emergency triggers (Section 6.4) are driven separately in Phase 5.
 *
 * Defaults encode the research finding that the +50% cohort peaks and collapses
 * within ~20 minutes: aggressive time stop, early-arming tight trailing stop.
 */
export type ExitCfg = Config['exits'];

export interface ExitState {
  entryPrice: number;
  openedAtMs: number;
  /** Highest price seen since open (caller keeps this current). */
  highWaterPrice: number;
  tp0Done: boolean;
  tp1Done: boolean;
  trailingArmed: boolean;
  highVolatility: boolean;
  /** Current hard-stop price (moves up to +tp1MoveStopToPct after TP1). */
  stopPrice: number;
}

export interface ExitDecision {
  trigger: ExitTrigger;
  /** Fraction of the ORIGINAL position to sell (1 == the full remainder). */
  sellFraction: number;
  reason: string;
}

export function gainPct(entryPrice: number, price: number): number {
  if (entryPrice <= 0) return 0;
  return (price / entryPrice - 1) * 100;
}

/**
 * Exit config for a position, tightened when the entry relied on widened
 * ("relaxed-risk") guardrail thresholds.
 *
 * Shared by the live position manager and the dry-run twin: the twin's whole
 * purpose is to isolate EXECUTION drag, so it must run the exact same exit
 * rules as live. If these two ever diverged, delta(live, dry) would silently
 * fold a strategy difference into a number read as execution cost.
 */
export function exitCfgFor(config: Config, relaxedRisk: boolean, overrides?: ExitOverrides): ExitCfg {
  // Overrides (twin experiment lane) apply BEFORE the relaxed-risk tightening,
  // so a relaxed accept is still tightened relative to the variant, exactly as
  // live tightens relative to the baseline.
  const base: ExitCfg = overrides ? ({ ...config.exits, ...definedOnly(overrides) } as ExitCfg) : config.exits;
  if (!relaxedRisk) return base;
  return {
    ...base,
    tp0Enabled: config.guardrails.relaxedRiskTp0Enabled || base.tp0Enabled,
    timeStopMinutes: Math.min(base.timeStopMinutes, config.guardrails.relaxedRiskTimeStopMinutes),
    trailingGapPct: Math.min(base.trailingGapPct, config.guardrails.relaxedRiskTrailingGapPct),
  };
}

function definedOnly<T extends object>(o: T): Partial<T> {
  const out: Partial<T> = {};
  for (const [k, v] of Object.entries(o)) if (v !== undefined) (out as Record<string, unknown>)[k] = v;
  return out;
}

export function evaluateExit(
  s: ExitState,
  price: number,
  nowMs: number,
  cfg: ExitCfg,
): ExitDecision | null {
  const gain = gainPct(s.entryPrice, price);
  const trailingGap = s.highVolatility ? cfg.trailingGapHighVolPct : cfg.trailingGapPct;

  // --- risk triggers first (a full exit of the remainder) ---
  if (price <= s.stopPrice) {
    return { trigger: 'STOP_LOSS', sellFraction: 1, reason: `price ${fmt(price)} <= stop ${fmt(s.stopPrice)}` };
  }
  if (s.trailingArmed && price <= s.highWaterPrice * (1 - trailingGap / 100)) {
    return {
      trigger: 'TRAILING_STOP',
      sellFraction: 1,
      reason: `price ${fmt(price)} <= ${trailingGap}% below high ${fmt(s.highWaterPrice)}`,
    };
  }
  if (!s.tp1Done && nowMs - s.openedAtMs >= cfg.timeStopMinutes * 60_000) {
    return { trigger: 'TIME_STOP', sellFraction: 1, reason: `no TP1 within ${cfg.timeStopMinutes}m` };
  }
  // Dead money: no partial banked yet and the peak never cleared the
  // follow-through bar by the deadline — free the slot rather than wait for
  // the time stop (or for the flat-then-zero rug pattern to resolve itself).
  if (
    cfg.deadMoneyEnabled &&
    !s.tp0Done &&
    !s.tp1Done &&
    nowMs - s.openedAtMs >= cfg.deadMoneyMinutes * 60_000 &&
    gainPct(s.entryPrice, s.highWaterPrice) < cfg.deadMoneyMaxMfePct
  ) {
    return {
      trigger: 'TIME_STOP',
      sellFraction: 1,
      reason: `dead money: peak +${gainPct(s.entryPrice, s.highWaterPrice).toFixed(1)}% < ${cfg.deadMoneyMaxMfePct}% after ${cfg.deadMoneyMinutes}m`,
    };
  }

  // --- profit triggers ---
  if (s.tp1Done && gain >= cfg.tp2Pct) {
    return { trigger: 'TAKE_PROFIT_2', sellFraction: 1, reason: `+${gain.toFixed(0)}% >= TP2 ${cfg.tp2Pct}%` };
  }
  if (!s.tp1Done && gain >= cfg.tp1Pct) {
    return {
      trigger: 'TAKE_PROFIT_1',
      sellFraction: cfg.tp1SellFraction,
      reason: `+${gain.toFixed(0)}% >= TP1 ${cfg.tp1Pct}%`,
    };
  }
  // Early partial (opt-in): banks some gain on tokens that reach tp0Pct but may
  // never hit TP1. Fires at most once, before TP1.
  if (cfg.tp0Enabled && !s.tp0Done && !s.tp1Done && gain >= cfg.tp0Pct) {
    return {
      trigger: 'TAKE_PROFIT_0',
      sellFraction: cfg.tp0SellFraction,
      reason: `+${gain.toFixed(0)}% >= TP0 ${cfg.tp0Pct}%`,
    };
  }

  return null;
}

function fmt(x: number): string {
  return x.toPrecision(4);
}
