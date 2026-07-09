import type { Config } from '../config/schema.ts';
import type { TypedBus } from '../core/bus.ts';
import type { Repositories } from '../persistence/repositories.ts';
import type { Mint, Position, PoolPricingRef, ExitTrigger } from '../core/types.ts';
import { LAMPORTS_PER_SOL } from '../core/constants.ts';
import { deriveAta } from '../core/ata.ts';
import { logger } from '../core/logger.ts';
import { PaperPosition, type Fill } from './position.ts';
import { computePrice, type PricePoller, type PriceTick } from './pricing.ts';
import { EmergencyMonitor } from './monitors.ts';
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
  /** Last observed price (for force-close when no fresh tick is available). */
  lastPrice: number;
  monitor: EmergencyMonitor;
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
  private readonly risk: { canEnter(): { ok: boolean; reason?: string; detail?: string } } | undefined;
  private unsubscribe: (() => void) | null = null;
  private unsubscribeKill: (() => void) | null = null;

  constructor(deps: {
    config: Config;
    bus: TypedBus;
    repos: Repositories;
    poller: PricePoller;
    executor?: Executor;
    risk?: { canEnter(): { ok: boolean; reason?: string; detail?: string } };
    now?: () => number;
  }) {
    this.config = deps.config;
    this.bus = deps.bus;
    this.repos = deps.repos;
    this.poller = deps.poller;
    this.executor = deps.executor;
    this.risk = deps.risk;
    this.now = deps.now ?? (() => Date.now());
  }

  start(): void {
    this.poller.setHandler((tick) => this.onTick(tick));
    this.poller.start();
    this.unsubscribe = this.bus.on('openPosition', (e) => this.open(e.mint, e.sizeSol, e.highVolatility, e.pricing));
    this.unsubscribeKill = this.bus.on('killSwitch', () => this.forceCloseAll('KILL_SWITCH', 'kill switch'));
    this.log.info('position manager started', { mode: this.config.mode });
  }

  stop(): void {
    this.unsubscribe?.();
    this.unsubscribeKill?.();
    this.unsubscribe = null;
    this.unsubscribeKill = null;
    this.poller.stop();
  }

  /** Flatten every open position at its last observed price (kill switch / shutdown). */
  forceCloseAll(trigger: ExitTrigger, detail?: string): void {
    const mints = [...this.positions.keys()];
    if (mints.length === 0) return;
    this.log.warn('force-closing all positions', { count: mints.length, trigger, detail });
    for (const mint of mints) {
      const rec = this.positions.get(mint);
      if (!rec) continue;
      const fill = rec.pos.forceClose(rec.lastPrice, this.now(), trigger);
      if (fill) {
        rec.fillCount++;
        if (this.executor) void this.executeExit(rec, fill);
        this.bus.emit('exitTriggered', { mint, trigger, detail: detail ?? 'force close' });
      }
      if (rec.pos.state === 'CLOSED') this.finalize(mint, rec, rec.lastPrice);
    }
  }

  /** Fire an EMERGENCY_EXIT, auto-blacklist the mint + creator, and alert. */
  private handleEmergency(mint: Mint, rec: PositionRecord, price: number, kind: string, detail: string): void {
    this.log.error('EMERGENCY EXIT', { mint, kind, detail });
    const fill = rec.pos.forceClose(price, this.now(), 'EMERGENCY_EXIT');
    if (fill) {
      rec.fillCount++;
      if (this.executor) void this.executeExit(rec, fill);
      this.bus.emit('exitTriggered', { mint, trigger: 'EMERGENCY_EXIT', detail });
    }
    // Auto-blacklist (Section 6.5): this creator/mint just rugged us.
    try {
      this.repos.blacklistMint(mint, kind);
      if (rec.pricing.creator) this.repos.blacklistCreator(rec.pricing.creator, kind);
    } catch (err) {
      this.log.error('auto-blacklist failed', { mint, err });
    }
    this.bus.emit('alert', {
      level: 'error',
      message: `🚨 EMERGENCY EXIT ${short(mint)} — ${kind}: ${detail} (creator blacklisted)`,
      telegram: true,
    });
    if (rec.pos.state === 'CLOSED') this.finalize(mint, rec, price);
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
    // Defense in depth: the risk manager also gates entry at H10, but re-check
    // here in case a breaker tripped between screening and the open.
    const decision = this.risk?.canEnter();
    if (decision && !decision.ok) {
      const reason = decision.reason === 'KILL_SWITCH' ? 'KILL_SWITCH' : 'CIRCUIT_BREAKER';
      this.bus.emit('entryVetoed', { mint, reason, detail: `${decision.reason}: ${decision.detail ?? ''}` });
      this.log.info('entry blocked — risk breaker', { mint, reason: decision.reason, detail: decision.detail });
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
    const monitor = new EmergencyMonitor({
      lpDropPct: this.config.exits.emergencyLpDropPct,
      windowTicks: this.config.exits.lpDropWindowTicks,
      creatorDumpEnabled: this.config.exits.creatorDumpEnabled,
      creatorDumpPct: this.config.exits.creatorDumpThresholdPct,
    });
    this.positions.set(mint, { pos, pricing, fillCount: 0, lastPrice: entryPrice, monitor });

    // Monitor the creator's base-token ATA for dev-dump (batched into the poll).
    let creatorAta: string | undefined;
    if (this.config.exits.creatorDumpEnabled && pricing.creator) {
      try {
        creatorAta = deriveAta(pricing.creator, pricing.baseMint, pricing.baseIsToken2022 ?? false);
      } catch (err) {
        this.log.warn('creator ATA derivation failed — dev-dump monitor disabled for this position', { mint, err });
      }
    }
    this.poller.register({
      mint,
      baseVault: pricing.baseVault,
      quoteVault: pricing.quoteVault,
      baseDecimals: pricing.baseDecimals,
      ...(creatorAta ? { creatorAta } : {}),
    });

    this.persist(pos, 'OPEN', entryPrice, sizeSol, openedAtMs, null, null, null);
    this.bus.emit('positionUpdate', this.toPosition(pos, 'OPEN', entryPrice, sizeSol, openedAtMs));
    this.bus.emit('alert', {
      level: 'info',
      message: `📈 opened ${short(mint)} — ${sizeSol.toFixed(3)} SOL @ ${entryPrice.toPrecision(4)}${highVolatility ? ' (high-vol)' : ''}`,
      telegram: true,
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

    // Persist the tick for replay/tuning (hourly prune handles retention).
    try {
      this.repos.insertPriceTick({
        mint: tick.mint,
        slot: null,
        price: tick.price,
        solReserve: Number(tick.quoteReserveLamports) / LAMPORTS_PER_SOL,
      });
    } catch (err) {
      this.log.debug('price tick persist failed', { mint: tick.mint, err });
    }

    rec.lastPrice = tick.price;

    // In-position emergency check (LP pull / creator dump) — worst-case exit.
    const signal = rec.monitor.onTick({
      quoteReserveLamports: tick.quoteReserveLamports,
      ...(tick.creatorBaseBalance !== undefined ? { creatorBaseBalance: tick.creatorBaseBalance } : {}),
    });
    if (signal && rec.pos.state === 'OPEN') {
      this.handleEmergency(tick.mint, rec, tick.price, signal.kind, signal.detail);
      return;
    }

    const fills = rec.pos.onPrice(tick.price, tick.atMs);
    for (const fill of fills) {
      rec.fillCount++;
      if (this.executor) void this.executeExit(rec, fill);
      this.bus.emit('exitTriggered', { mint: tick.mint, trigger: fill.trigger, detail: fill.reason });
      this.bus.emit('alert', {
        level: 'info',
        message:
          `↗ exit ${short(tick.mint)} — ${fill.trigger} ${Math.round(fill.fraction * 100)}% ` +
          `· pnl ${fill.pnlSol >= 0 ? '+' : ''}${fill.pnlSol.toFixed(4)} SOL`,
        telegram: true,
      });
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
      telegram: true,
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
