import type { ExitTrigger, PositionState } from '../core/types.ts';
import { evaluateExit, gainPct, type ExitCfg, type ExitState } from '../exits/engine.ts';

/**
 * A single paper position and its FSM (Section 7.3):
 *   PENDING_ENTRY -> OPEN -> EXITING -> CLOSED
 *
 * Drives exit triggers off local price ticks and simulates fills. Partial TP1
 * (sell a fraction, move the remainder's stop up) is modelled by tracking the
 * remaining fraction of the original size. Realised PnL accumulates across
 * fills; fee drag is applied by the manager at close.
 */

export interface Fill {
  trigger: ExitTrigger;
  /** Fraction of the original position sold in this fill. */
  fraction: number;
  price: number;
  gainPct: number;
  pnlSol: number;
  reason: string;
}

const EPSILON = 1e-9;

export class PaperPosition {
  readonly mint: string;
  readonly sizeSol: number;
  readonly entryPrice: number;
  readonly openedAtMs: number;
  private readonly cfg: ExitCfg;
  private readonly highVolatility: boolean;

  state: PositionState = 'OPEN';
  private remaining = 1; // fraction of original still held
  private highWaterPrice: number;
  private trailingArmed = false;
  private tp1Done = false;
  private stopPrice: number;
  realizedPnlSol = 0;
  closedAtMs?: number;
  lastTrigger?: ExitTrigger;

  constructor(args: {
    mint: string;
    sizeSol: number;
    entryPrice: number;
    openedAtMs: number;
    highVolatility: boolean;
    cfg: ExitCfg;
  }) {
    this.mint = args.mint;
    this.sizeSol = args.sizeSol;
    this.entryPrice = args.entryPrice;
    this.openedAtMs = args.openedAtMs;
    this.highVolatility = args.highVolatility;
    this.cfg = args.cfg;
    this.highWaterPrice = args.entryPrice;
    this.stopPrice = args.entryPrice * (1 - args.cfg.hardStopPct / 100);
  }

  /** Feed a price tick; returns any fills that occurred. */
  onPrice(price: number, nowMs: number): Fill[] {
    if (this.state !== 'OPEN' || price <= 0) return [];

    if (price > this.highWaterPrice) this.highWaterPrice = price;
    if (!this.trailingArmed && gainPct(this.entryPrice, price) >= this.cfg.trailingArmPct) {
      this.trailingArmed = true;
    }

    const decision = evaluateExit(this.snapshot(), price, nowMs, this.cfg);
    if (!decision) return [];

    const fraction = Math.min(decision.sellFraction, this.remaining);
    if (fraction <= EPSILON) return [];

    const pnlSol = fraction * this.sizeSol * (price / this.entryPrice - 1);
    this.realizedPnlSol += pnlSol;
    this.remaining -= fraction;
    this.lastTrigger = decision.trigger;

    if (decision.trigger === 'TAKE_PROFIT_1') {
      this.tp1Done = true;
      // Move the remainder's stop up to lock in gains (Section 7.3).
      this.stopPrice = this.entryPrice * (1 + this.cfg.tp1MoveStopToPct / 100);
    }

    const fill: Fill = {
      trigger: decision.trigger,
      fraction,
      price,
      gainPct: gainPct(this.entryPrice, price),
      pnlSol,
      reason: decision.reason,
    };

    if (this.remaining <= EPSILON) {
      this.state = 'CLOSED';
      this.closedAtMs = nowMs;
    }
    return [fill];
  }

  /** Force-close the remainder at a price (kill switch / shutdown). */
  forceClose(price: number, nowMs: number, trigger: ExitTrigger): Fill | null {
    if (this.state !== 'OPEN' || this.remaining <= EPSILON) return null;
    const fraction = this.remaining;
    const pnlSol = fraction * this.sizeSol * (price / this.entryPrice - 1);
    this.realizedPnlSol += pnlSol;
    this.remaining = 0;
    this.state = 'CLOSED';
    this.closedAtMs = nowMs;
    this.lastTrigger = trigger;
    return { trigger, fraction, price, gainPct: gainPct(this.entryPrice, price), pnlSol, reason: 'force close' };
  }

  get remainingFraction(): number {
    return this.remaining;
  }

  private snapshot(): ExitState {
    return {
      entryPrice: this.entryPrice,
      openedAtMs: this.openedAtMs,
      highWaterPrice: this.highWaterPrice,
      tp1Done: this.tp1Done,
      trailingArmed: this.trailingArmed,
      highVolatility: this.highVolatility,
      stopPrice: this.stopPrice,
    };
  }
}
