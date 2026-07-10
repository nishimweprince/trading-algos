import type { ExitTrigger } from '../core/types.ts';
import type { Config } from '../config/schema.ts';

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
