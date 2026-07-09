import type { Config } from '../config/schema.ts';
import type { TypedBus } from '../core/bus.ts';
import type { Repositories } from '../persistence/repositories.ts';
import { LAMPORTS_PER_SOL } from '../core/constants.ts';
import { logger } from '../core/logger.ts';

/**
 * Risk manager and circuit breakers (Section 8). Consumes CLOSED position events
 * to track realized daily PnL (UTC-reset), consecutive losses, and the 24h
 * emergency-exit count; tracks feed health and the kill switch; and gates every
 * entry via `canEnter()`. Breaker transitions emit a `breaker` bus event (the
 * dashboard already renders these) and persist to `breaker_events`.
 *
 * Counters rehydrate from the DB on start so a restart cannot wash out a
 * tripped breaker.
 */

export type BreakerType =
  | 'DAILY_LOSS'
  | 'CONSECUTIVE_LOSSES'
  | 'EMERGENCY_EXITS'
  | 'WALLET_FLOOR'
  | 'STREAM_DOWN'
  | 'KILL_SWITCH';

export interface EntryDecision {
  ok: boolean;
  reason?: BreakerType;
  detail?: string;
}

const DAY_MS = 86_400_000;
// Priority order for the single reason reported to callers (most severe first).
const REASON_ORDER: BreakerType[] = [
  'KILL_SWITCH',
  'STREAM_DOWN',
  'WALLET_FLOOR',
  'DAILY_LOSS',
  'CONSECUTIVE_LOSSES',
  'EMERGENCY_EXITS',
];

export interface RiskManagerDeps {
  config: Config;
  bus: TypedBus;
  repos: Repositories;
  /** Live/dry-run only — absent in paper (no wallet-floor / pct-of-wallet cap). */
  getWalletBalanceLamports?: () => Promise<bigint>;
  now?: () => number;
}

export class RiskManager {
  private readonly config: Config;
  private readonly bus: TypedBus;
  private readonly repos: Repositories;
  private readonly getWalletBalanceLamports: (() => Promise<bigint>) | undefined;
  private readonly now: () => number;
  private readonly log = logger.child({ mod: 'risk' });

  private currentDay = '';
  private dailyRealizedPnlSol = 0;
  private consecutiveLosses = 0;
  private consecutiveHaltUntilMs = 0;
  private emergencyExitTimes: number[] = [];
  private walletBalanceLamports: bigint | null = null;
  private streamDown = false;
  private killedFlag = false;
  private readonly tripped = new Set<BreakerType>();
  private unsubs: Array<() => void> = [];

  constructor(deps: RiskManagerDeps) {
    this.config = deps.config;
    this.bus = deps.bus;
    this.repos = deps.repos;
    this.getWalletBalanceLamports = deps.getWalletBalanceLamports;
    this.now = deps.now ?? (() => Date.now());
  }

  start(): void {
    this.currentDay = this.dayOf(this.now());
    this.rehydrate();
    this.unsubs.push(
      this.bus.on('positionUpdate', (p) => {
        if (p.state === 'CLOSED' && typeof p.pnlSol === 'number') {
          this.onClosed(p.pnlSol, p.exitTrigger === 'EMERGENCY_EXIT');
        }
      }),
      this.bus.on('streamHealth', (s) => {
        if (s.source !== 'detector') return;
        this.streamDown = !s.healthy;
        this.reconcile();
      }),
      this.bus.on('killSwitch', (k) => this.engageKillSwitch(k.source, k.detail)),
    );
    this.log.info('risk manager started', { day: this.currentDay });
  }

  stop(): void {
    for (const u of this.unsubs.splice(0)) u();
  }

  get killed(): boolean {
    return this.killedFlag;
  }

  /** Refresh the cached wallet balance (call once per entry attempt). */
  async refreshWalletBalance(): Promise<void> {
    if (!this.getWalletBalanceLamports) return;
    try {
      this.walletBalanceLamports = await this.getWalletBalanceLamports();
    } catch (err) {
      this.log.warn('wallet balance refresh failed — floor check will use last known', { err });
    }
  }

  /** Synchronous entry gate consulted by H10 and the position manager. */
  canEnter(): EntryDecision {
    this.maybeResetDay();
    this.reconcile();
    for (const reason of REASON_ORDER) {
      if (this.tripped.has(reason)) return { ok: false, reason, detail: this.detailFor(reason) };
    }
    return { ok: true };
  }

  engageKillSwitch(source: 'telegram' | 'file' | 'internal', detail?: string): void {
    if (this.killedFlag) return;
    this.killedFlag = true;
    this.log.error('KILL SWITCH engaged', { source, detail });
    this.reconcile();
  }

  statusSummary(): string {
    const bal = this.walletBalanceLamports === null ? 'n/a' : (Number(this.walletBalanceLamports) / LAMPORTS_PER_SOL).toFixed(3);
    const trips = this.tripped.size ? [...this.tripped].join(',') : 'none';
    return (
      `mode ${this.config.mode} | killed ${this.killedFlag} | streamDown ${this.streamDown} | ` +
      `dayPnL ${this.dailyRealizedPnlSol.toFixed(4)} SOL | consecLosses ${this.consecutiveLosses} | ` +
      `emergencies24h ${this.emergencyExitCount()} | wallet ${bal} SOL | tripped ${trips}`
    );
  }

  // --- internals ---

  private onClosed(pnlSol: number, wasEmergency: boolean): void {
    this.maybeResetDay();
    this.dailyRealizedPnlSol += pnlSol;
    if (pnlSol < 0) {
      this.consecutiveLosses += 1;
      if (this.consecutiveLosses >= this.config.risk.consecutiveLossHalt) {
        this.consecutiveHaltUntilMs = this.now() + this.consecutiveLossHaltMinutes() * 60_000;
      }
    } else {
      this.consecutiveLosses = 0;
      this.consecutiveHaltUntilMs = 0;
    }
    if (wasEmergency) this.emergencyExitTimes.push(this.now());
    this.reconcile();
  }

  private reconcile(): void {
    const next = this.computeTripped();
    for (const [type, detail] of next) {
      if (!this.tripped.has(type)) this.emitBreaker(type, true, detail);
    }
    for (const type of [...this.tripped]) {
      if (!next.has(type)) this.emitBreaker(type, false, 'cleared');
    }
    this.tripped.clear();
    for (const type of next.keys()) this.tripped.add(type);
  }

  private computeTripped(): Map<BreakerType, string> {
    const t = new Map<BreakerType, string>();
    const now = this.now();
    if (this.killedFlag) t.set('KILL_SWITCH', 'kill switch engaged');
    if (this.streamDown) t.set('STREAM_DOWN', 'detection feed down');
    if (now < this.consecutiveHaltUntilMs) {
      t.set('CONSECUTIVE_LOSSES', `${this.consecutiveLosses} losses; halt ${Math.ceil((this.consecutiveHaltUntilMs - now) / 60_000)}m`);
    }
    if (this.emergencyExitCount() >= this.config.risk.emergencyExitCount24h) {
      t.set('EMERGENCY_EXITS', `${this.emergencyExitCount()} in 24h`);
    }
    const dailyLimit = this.dailyLossLimitSol();
    if (this.dailyRealizedPnlSol <= -dailyLimit) {
      t.set('DAILY_LOSS', `${this.dailyRealizedPnlSol.toFixed(4)} SOL <= -${dailyLimit.toFixed(4)}`);
    }
    if (this.walletBalanceLamports !== null) {
      const balSol = Number(this.walletBalanceLamports) / LAMPORTS_PER_SOL;
      const floor = this.config.wallet.balanceFloorSol + this.config.entry.baseSizeSol;
      if (balSol < floor) t.set('WALLET_FLOOR', `${balSol.toFixed(3)} SOL < ${floor.toFixed(3)} floor+size`);
    }
    return t;
  }

  private dailyLossLimitSol(): number {
    const abs = this.config.risk.dailyLossLimitSol;
    if (this.walletBalanceLamports === null) return abs;
    const pctLimit = (Number(this.walletBalanceLamports) / LAMPORTS_PER_SOL) * (this.config.risk.dailyLossLimitWalletPct / 100);
    return Math.min(abs, pctLimit);
  }

  private emergencyExitCount(): number {
    const cutoff = this.now() - DAY_MS;
    this.emergencyExitTimes = this.emergencyExitTimes.filter((t) => t >= cutoff);
    return this.emergencyExitTimes.length;
  }

  private emitBreaker(type: BreakerType, tripped: boolean, detail: string): void {
    this.bus.emit('breaker', { type, tripped, detail });
    try {
      this.repos.recordBreakerEvent(type, tripped, detail);
    } catch (err) {
      this.log.error('failed to persist breaker event', { type, err });
    }
    this.bus.emit('alert', {
      level: tripped ? 'error' : 'info',
      message: `${tripped ? '⛔ breaker TRIPPED' : '✅ breaker cleared'}: ${type} — ${detail}`,
      telegram: true,
    });
    this.log.warn('breaker transition', { type, tripped, detail });
  }

  private detailFor(reason: BreakerType): string {
    return this.computeTripped().get(reason) ?? '';
  }

  private maybeResetDay(): void {
    const day = this.dayOf(this.now());
    if (day !== this.currentDay) {
      this.currentDay = day;
      this.dailyRealizedPnlSol = 0;
      this.reconcile(); // clears a DAILY_LOSS trip at the UTC boundary
    }
  }

  private dayOf(ms: number): string {
    return new Date(ms).toISOString().slice(0, 10);
  }

  private rehydrate(): void {
    const midnightIso = `${this.currentDay}T00:00:00Z`;
    this.dailyRealizedPnlSol = this.repos.sumRealizedPnlSince(midnightIso);
    // Consecutive losses: count leading negatives among the most recent closes.
    const recent = this.repos.recentClosedPnls(this.config.risk.consecutiveLossHalt);
    let streak = 0;
    for (const pnl of recent) {
      if (pnl < 0) streak += 1;
      else break;
    }
    this.consecutiveLosses = streak;
    if (streak >= this.config.risk.consecutiveLossHalt) {
      this.consecutiveHaltUntilMs = this.now() + this.consecutiveLossHaltMinutes() * 60_000;
    }
    // Emergency exits in the last 24h — timestamps unknown, so seed at `now`
    // (conservative: they age out over the next 24h rather than immediately).
    const emergencies = this.repos.countClosedByTriggerSince('EMERGENCY_EXIT', new Date(this.now() - DAY_MS).toISOString());
    this.emergencyExitTimes = Array.from({ length: emergencies }, () => this.now());
    this.reconcile();
    this.log.info('risk counters rehydrated', {
      dayPnlSol: Number(this.dailyRealizedPnlSol.toFixed(4)),
      consecutiveLosses: this.consecutiveLosses,
      emergencies24h: emergencies,
    });
  }

  private consecutiveLossHaltMinutes(): number {
    return this.config.mode === 'dry-run'
      ? this.config.risk.dryRunConsecutiveLossHaltMinutes
      : this.config.risk.consecutiveLossHaltMinutes;
  }
}
