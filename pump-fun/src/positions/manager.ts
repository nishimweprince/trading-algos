import type { Config } from '../config/schema.ts';
import type { TypedBus } from '../core/bus.ts';
import type { Repositories } from '../persistence/repositories.ts';
import type { Mint, Position, PoolPricingRef } from '../core/types.ts';
import { logger } from '../core/logger.ts';
import { PaperPosition, type Fill } from './position.ts';
import { computePrice, type PricePoller, type PriceTick } from './pricing.ts';
import type { Executor } from '../executor/index.ts';

/**
 * Position manager (Section 7.3). In paper mode it opens a simulated position
 * for each accepted candidate, drives the exit FSM off local price ticks, and
 * records fee-adjusted PnL. Concurrency is capped here (a minimal guard; the
 * full risk manager — daily loss, consecutive losses, kill switch — is Phase 5).
 *
 * The live executor (Phase 4) will replace the simulated entry/exit fills with
 * real transactions behind this same lifecycle.
 */

interface PositionRecord {
  pos: PaperPosition;
  pricing: PoolPricingRef;
  fillCount: number;
}

export class PositionManager {
  private readonly config: Config;
  private readonly bus: TypedBus;
  private readonly repos: Repositories;
  private readonly poller: PricePoller;
  private readonly log = logger.child({ mod: 'positions' });
  private readonly positions = new Map<Mint, PositionRecord>();
  private readonly now: () => number;
  /** dry-run/live: builds + broadcasts real buy/sell txs alongside the FSM. */
  private readonly executor: Executor | undefined;
  private unsubscribe: (() => void) | null = null;

  constructor(deps: {
    config: Config;
    bus: TypedBus;
    repos: Repositories;
    poller: PricePoller;
    executor?: Executor;
    now?: () => number;
  }) {
    this.config = deps.config;
    this.bus = deps.bus;
    this.repos = deps.repos;
    this.poller = deps.poller;
    this.executor = deps.executor;
    this.now = deps.now ?? (() => Date.now());
  }

  start(): void {
    this.poller.setHandler((tick) => this.onTick(tick));
    this.poller.start();
    this.unsubscribe = this.bus.on('openPosition', (e) => this.open(e.mint, e.sizeSol, e.highVolatility, e.pricing));
    this.log.info('position manager started', { mode: this.config.mode });
  }

  stop(): void {
    this.unsubscribe?.();
    this.unsubscribe = null;
    this.poller.stop();
  }

  get openCount(): number {
    return this.positions.size;
  }

  private open(mint: Mint, sizeSol: number, highVolatility: boolean, pricing: PoolPricingRef): void {
    if (this.positions.has(mint)) return;
    if (this.positions.size >= this.config.risk.maxConcurrentPositions) {
      this.bus.emit('entryVetoed', { mint, reason: 'CIRCUIT_BREAKER', detail: 'max concurrent positions' });
      this.log.info('entry blocked — max concurrent positions', { mint, cap: this.config.risk.maxConcurrentPositions });
      return;
    }

    const entryPrice = computePrice(pricing.baseReserve, pricing.quoteReserveLamports, pricing.baseDecimals);
    if (entryPrice <= 0) {
      this.log.warn('cannot open — invalid entry price', { mint });
      return;
    }

    const openedAtMs = this.now();
    const pos = new PaperPosition({
      mint,
      sizeSol,
      entryPrice,
      openedAtMs,
      highVolatility,
      cfg: this.config.exits,
    });
    this.positions.set(mint, { pos, pricing, fillCount: 0 });
    this.poller.register({
      mint,
      baseVault: pricing.baseVault,
      quoteVault: pricing.quoteVault,
      baseDecimals: pricing.baseDecimals,
    });

    this.persist(pos, 'OPEN', entryPrice, sizeSol, openedAtMs, null, null, null);
    this.bus.emit('positionUpdate', this.toPosition(pos, 'OPEN', entryPrice, sizeSol, openedAtMs));
    this.bus.emit('alert', {
      level: 'info',
      message: `📈 opened ${short(mint)} — ${sizeSol.toFixed(3)} SOL @ ${entryPrice.toPrecision(4)}${highVolatility ? ' (high-vol)' : ''}`,
    });
    this.log.info('paper position opened', { mint, sizeSol, entryPrice, openCount: this.positions.size });

    // dry-run/live: build + broadcast the real buy (transcript in dry-run, send
    // in live). Fire-and-log; paper accounting drives the FSM either way.
    if (this.executor) void this.executeEntry(mint, pricing, sizeSol);
  }

  private async executeEntry(mint: Mint, pricing: PoolPricingRef, sizeSol: number): Promise<void> {
    try {
      await this.executor!.buy(pricing.poolAddress, pricing.baseMint, sizeSol);
    } catch (err) {
      this.log.error('entry execution failed', { mint, err });
    }
  }

  private async executeExit(rec: PositionRecord, fill: Fill): Promise<void> {
    // Raw base-token units for this fill's fraction of the original position.
    const wholeTokens = rec.pos.sizeSol / rec.pos.entryPrice;
    const rawBase = BigInt(Math.floor(wholeTokens * fill.fraction * 10 ** rec.pricing.baseDecimals));
    if (rawBase <= 0n) return;
    try {
      await this.executor!.sell(rec.pricing.poolAddress, rec.pricing.baseMint, rawBase, this.config.entry.maxSlippagePct);
    } catch (err) {
      this.log.error('exit execution failed', { mint: rec.pos.mint, err });
    }
  }

  private onTick(tick: PriceTick): void {
    const rec = this.positions.get(tick.mint);
    if (!rec) return;

    const fills = rec.pos.onPrice(tick.price, tick.atMs);
    for (const fill of fills) {
      rec.fillCount++;
      if (this.executor) void this.executeExit(rec, fill);
      this.bus.emit('exitTriggered', { mint: tick.mint, trigger: fill.trigger, detail: fill.reason });
      this.log.info('exit fill', {
        mint: tick.mint,
        trigger: fill.trigger,
        fraction: Number(fill.fraction.toFixed(3)),
        gainPct: Number(fill.gainPct.toFixed(1)),
        pnlSol: Number(fill.pnlSol.toFixed(5)),
      });
    }

    if (rec.pos.state === 'CLOSED') this.finalize(tick.mint, rec, tick.price);
  }

  private finalize(mint: Mint, rec: PositionRecord, exitPrice: number): void {
    const gross = rec.pos.realizedPnlSol;
    const fees = this.estimateFees(rec.pos.sizeSol, rec.fillCount);
    const net = gross - fees;
    const pnlPct = (net / rec.pos.sizeSol) * 100;
    const closedAt = rec.pos.closedAtMs ?? this.now();

    this.poller.unregister(mint);
    this.positions.delete(mint);

    const position: Position = {
      mint,
      state: 'CLOSED',
      sizeSol: rec.pos.sizeSol,
      entryPrice: rec.pos.entryPrice,
      openedAt: rec.pos.openedAtMs,
      closedAt,
      ...(rec.pos.lastTrigger ? { exitTrigger: rec.pos.lastTrigger } : {}),
      pnlSol: net,
      pnlPct,
    };
    this.persist(rec.pos, 'CLOSED', rec.pos.entryPrice, rec.pos.sizeSol, rec.pos.openedAtMs, closedAt, net, pnlPct);
    this.bus.emit('positionUpdate', position);

    const emoji = net >= 0 ? '✅' : '🔻';
    this.bus.emit('alert', {
      level: 'info',
      message: `${emoji} closed ${short(mint)} — ${rec.pos.lastTrigger} · net ${net >= 0 ? '+' : ''}${net.toFixed(4)} SOL (${pnlPct >= 0 ? '+' : ''}${pnlPct.toFixed(1)}%, gross ${gross.toFixed(4)}, fees ${fees.toFixed(4)})`,
    });
    this.log.info('paper position closed', {
      mint,
      trigger: rec.pos.lastTrigger,
      grossPnlSol: Number(gross.toFixed(5)),
      feesSol: Number(fees.toFixed(5)),
      netPnlSol: Number(net.toFixed(5)),
      pnlPct: Number(pnlPct.toFixed(1)),
      exitPrice,
    });
  }

  /** Paper fee drag: priority+tip per tx (entry + each exit) and swap fee per leg. */
  private estimateFees(sizeSol: number, exitFills: number): number {
    const txCount = 1 + Math.max(1, exitFills);
    const tip = this.config.fees.estPriorityTipSolPerTx * txCount;
    const swap = (this.config.fees.swapFeePct / 100) * sizeSol * 2; // entry + exit legs
    return tip + swap;
  }

  private toPosition(
    pos: PaperPosition,
    state: Position['state'],
    entryPrice: number,
    sizeSol: number,
    openedAt: number,
  ): Position {
    return { mint: pos.mint, state, sizeSol, entryPrice, openedAt };
  }

  private persist(
    pos: PaperPosition,
    state: Position['state'],
    entryPrice: number,
    sizeSol: number,
    openedAt: number,
    closedAt: number | null,
    pnlSol: number | null,
    pnlPct: number | null,
  ): void {
    try {
      const p: Position = {
        mint: pos.mint,
        state,
        sizeSol,
        entryPrice,
        openedAt,
        ...(closedAt ? { closedAt } : {}),
        ...(pos.lastTrigger ? { exitTrigger: pos.lastTrigger } : {}),
        ...(pnlSol !== null ? { pnlSol } : {}),
        ...(pnlPct !== null ? { pnlPct } : {}),
      };
      this.repos.upsertPosition(p);
    } catch (err) {
      this.log.error('failed to persist position', { mint: pos.mint, state, err });
    }
  }
}

function short(mint: string): string {
  return mint.length > 10 ? `${mint.slice(0, 4)}…${mint.slice(-4)}` : mint;
}
