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
  private tp0Done = false;
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
    const fill = this.previewPriceExit(price, nowMs);
    if (!fill) return [];
    this.applyFill(fill, nowMs);
    return [fill];
  }

  /**
   * Live mode uses this to discover an exit trigger without mutating the held
   * fraction. Price-observation state (high-water / trailing armed) still
   * advances because those are facts, not fills.
   */
  previewPriceExit(price: number, nowMs: number): Fill | null {
    if (this.state !== 'OPEN' || price <= 0) return null;
    this.observePrice(price);

    const decision = evaluateExit(this.snapshot(), price, nowMs, this.cfg);
    if (!decision) return null;

    const fraction = Math.min(decision.sellFraction, this.remaining);
    if (fraction <= EPSILON) return null;
    return this.makeFill(decision.trigger, fraction, price, decision.reason);
  }

  /** Force-close the remainder at a price (kill switch / shutdown). */
  forceClose(price: number, nowMs: number, trigger: ExitTrigger): Fill | null {
    const fill = this.previewForceClose(price, trigger);
    if (!fill) return null;
    this.applyFill(fill, nowMs);
    return fill;
  }

  /** Build a force-close fill without mutating. */
  previewForceClose(price: number, trigger: ExitTrigger): Fill | null {
    if (this.state !== 'OPEN' || this.remaining <= EPSILON) return null;
    return this.makeFill(trigger, this.remaining, price, 'force close');
  }

  /** Apply a fill that has actually completed. Live calls this after confirm. */
  applyFill(fill: Fill, nowMs: number): void {
    if (this.state !== 'OPEN' || fill.fraction <= EPSILON) return;
    this.realizedPnlSol += fill.pnlSol;
    this.remaining -= fill.fraction;
    this.lastTrigger = fill.trigger;

    if (fill.trigger === 'TAKE_PROFIT_0') {
      this.tp0Done = true;
      // Raise the stop to lock a floor on the remainder; never lower it.
      this.stopPrice = Math.max(this.stopPrice, this.entryPrice * (1 + this.cfg.tp0MoveStopToPct / 100));
    }

    if (fill.trigger === 'TAKE_PROFIT_1') {
      this.tp1Done = true;
      this.stopPrice = this.entryPrice * (1 + this.cfg.tp1MoveStopToPct / 100);
    }

    if (this.remaining <= EPSILON) {
      this.remaining = 0;
      this.state = 'CLOSED';
      this.closedAtMs = nowMs;
    }
  }

  get remainingFraction(): number {
    return this.remaining;
  }

  private snapshot(): ExitState {
    return {
      entryPrice: this.entryPrice,
      openedAtMs: this.openedAtMs,
      highWaterPrice: this.highWaterPrice,
      tp0Done: this.tp0Done,
      tp1Done: this.tp1Done,
      trailingArmed: this.trailingArmed,
      highVolatility: this.highVolatility,
      stopPrice: this.stopPrice,
    };
  }

  private observePrice(price: number): void {
    if (price > this.highWaterPrice) this.highWaterPrice = price;
    if (!this.trailingArmed && gainPct(this.entryPrice, price) >= this.cfg.trailingArmPct) {
      this.trailingArmed = true;
    }
  }

  private makeFill(trigger: ExitTrigger, fraction: number, price: number, reason: string): Fill {
    return {
      trigger,
      fraction,
      price,
      gainPct: gainPct(this.entryPrice, price),
      pnlSol: fraction * this.sizeSol * (price / this.entryPrice - 1),
      reason,
    };
  }
}
