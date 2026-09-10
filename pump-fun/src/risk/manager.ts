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

export interface RiskSnapshot {
  mode: Config['mode'];
  killed: boolean;
  streamDown: boolean;
  dayUtc: string;
  dailyRealizedPnlSol: number;
  dailyLossLimitSol: number;
  dailyLossUsedPct: number;
  consecutiveLosses: number;
  consecutiveLossHalt: number;
  consecutiveHaltUntilMs: number | null;
  emergencies24h: number;
  emergencyExitCount24hLimit: number;
  walletBalanceSol: number | null;
  walletFloorSol: number;
  tripped: BreakerType[];
  canEnter: EntryDecision;
}

const DAY_MS = 86_400_000;

/**
 * How stale a wallet-balance read may be before WALLET_FLOOR trips.
 *
 * refreshWalletBalance() runs once per screening plus on the periodic timer
 * below, so under a healthy RPC the cache is refreshed constantly and never
 * bites. It only engages when getBalance has been failing for minutes —
 * exactly when entering blind is most dangerous.
 */
const WALLET_BALANCE_MAX_STALE_MS = 120_000;
/**
 * Periodic wallet-balance refresh. Screenings alone cannot be trusted to keep
 * the cache fresh: on a quiet chain (devnet) minutes can pass with no
 * graduations, and without this timer the cache goes stale and WALLET_FLOOR
 * trips fail-closed even though the RPC and funding are both fine. Kept well
 * under WALLET_BALANCE_MAX_STALE_MS.
 */
const WALLET_BALANCE_REFRESH_MS = 60_000;
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
  private walletBalanceAtMs = 0;
  private walletRefreshTimer: NodeJS.Timeout | null = null;
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
    if (this.getWalletBalanceLamports && !this.walletRefreshTimer) {
      const timer = setInterval(() => {
        void this.refreshWalletBalance();
      }, WALLET_BALANCE_REFRESH_MS);
      timer.unref(); // never hold the process open on its own
      this.walletRefreshTimer = timer;
    }
    this.log.info('risk manager started', { day: this.currentDay });
  }

  stop(): void {
    for (const u of this.unsubs.splice(0)) u();
    if (this.walletRefreshTimer) {
      clearInterval(this.walletRefreshTimer);
      this.walletRefreshTimer = null;
    }
  }

  get killed(): boolean {
    return this.killedFlag;
  }

  /** Refresh the cached wallet balance (call once per entry attempt). */
  async refreshWalletBalance(): Promise<void> {
    if (!this.getWalletBalanceLamports) return;
    try {
      this.walletBalanceLamports = await this.getWalletBalanceLamports();
      this.walletBalanceAtMs = this.now();
    } catch (err) {
      this.log.warn('wallet balance refresh failed — WALLET_FLOOR will trip if it goes stale', { err });
    }
  }

  /**
   * True when we cannot currently vouch for the wallet balance: a provider
   * exists but we have never read it, or the last good read is too old.
   */
  private walletBalanceUnknown(): boolean {
    if (!this.getWalletBalanceLamports) return false; // paper: no wallet to check
    if (this.walletBalanceLamports === null) return true;
    return this.now() - this.walletBalanceAtMs > WALLET_BALANCE_MAX_STALE_MS;
  }

  /** Synchronous entry gate consulted by H10 and the position manager. */
  canEnter(): EntryDecision {
    // Master switch (risk.disableAllBreakers, testing only): every entry gate
    // passes and no breaker state is computed.
    if (this.config.risk.disableAllBreakers) return { ok: true };
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
    const trips = this.config.risk.disableAllBreakers
      ? 'disabled'
      : this.tripped.size
        ? [...this.tripped].join(',')
        : 'none';
    return (
      `mode ${this.config.mode} | killed ${this.killedFlag} | streamDown ${this.streamDown} | ` +
      `dayPnL ${this.dailyRealizedPnlSol.toFixed(4)} SOL | consecLosses ${this.consecutiveLosses} | ` +
      `emergencies24h ${this.emergencyExitCount()} | wallet ${bal} SOL | tripped ${trips}`
    );
  }

  /**
   * Minimum wallet balance that still permits entries: the gas floor plus the
   * minimum position size (entry.minSizeSol, from MIN_POSITION_SOL, ~$20), so
   * a wallet that cannot fund a full minimum-size position blocks entry with
   * an explicit insufficient-balance message instead of sizing down.
   */
  requiredBalanceSol(): number {
    return this.config.wallet.balanceFloorSol + Math.max(this.config.entry.baseSizeSol, this.config.entry.minSizeSol);
  }

  /** Live risk counters for the operator dashboard / ops report. */
  getSnapshot(): RiskSnapshot {
    this.maybeResetDay();
    const dailyLossLimitSol = this.dailyLossLimitSol();
    const balSol =
      this.walletBalanceLamports === null ? null : Number(this.walletBalanceLamports) / LAMPORTS_PER_SOL;
    const walletFloorSol = this.requiredBalanceSol();
    return {
      mode: this.config.mode,
      killed: this.killedFlag,
      streamDown: this.streamDown,
      dayUtc: this.currentDay,
      dailyRealizedPnlSol: this.dailyRealizedPnlSol,
      dailyLossLimitSol,
      dailyLossUsedPct: dailyLossLimitSol > 0 ? Math.min(100, (Math.max(0, -this.dailyRealizedPnlSol) / dailyLossLimitSol) * 100) : 0,
      consecutiveLosses: this.consecutiveLosses,
      consecutiveLossHalt: this.config.risk.consecutiveLossHalt,
      consecutiveHaltUntilMs: this.consecutiveHaltUntilMs > this.now() ? this.consecutiveHaltUntilMs : null,
      emergencies24h: this.emergencyExitCount(),
      emergencyExitCount24hLimit: this.config.risk.emergencyExitCount24h,
      walletBalanceSol: balSol,
      walletFloorSol,
      tripped: [...this.tripped],
      canEnter: this.canEnter(),
    };
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
    // Master switch: clear any latched trips (emitting cleared transitions so
    // the dashboard reflects it) and never trip while disabled.
    if (this.config.risk.disableAllBreakers) {
      for (const type of [...this.tripped]) {
        this.emitBreaker(type, false, 'cleared (breakers disabled)');
      }
      this.tripped.clear();
      return;
    }
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
    // Master switch (risk.disableAllBreakers, testing only): no breaker trips.
    if (this.config.risk.disableAllBreakers) return t;
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
    // Fail CLOSED on an unverifiable balance. Previously a balance that was
    // never fetched left walletBalanceLamports null and the floor check simply
    // did not run — so a rate-limited getBalance silently disabled a real
    // safety breaker and let entries through unchecked.
    if (this.walletBalanceUnknown()) {
      t.set('WALLET_FLOOR', 'wallet balance unavailable — cannot verify gas floor');
    } else if (this.walletBalanceLamports !== null) {
      const balSol = Number(this.walletBalanceLamports) / LAMPORTS_PER_SOL;
      const floor = this.requiredBalanceSol();
      if (balSol < floor) {
        t.set(
          'WALLET_FLOOR',
          `available balance ${balSol.toFixed(3)} SOL is below the required ${floor.toFixed(3)} SOL ` +
            `(gas floor ${this.config.wallet.balanceFloorSol.toFixed(3)} + min position size ${Math.max(this.config.entry.baseSizeSol, this.config.entry.minSizeSol).toFixed(3)}) — entries blocked until funded`,
        );
      }
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
    // When rehydrating, respect the actual most-recent close time instead of
    // restarting a full halt window on every process boot.
    const recent = this.repos.recentClosedPnlRecords(this.config.risk.consecutiveLossHalt);
    let streak = 0;
    for (const row of recent) {
      if (row.pnlSol < 0) streak += 1;
      else break;
    }
    this.consecutiveLosses = streak;
    if (streak >= this.config.risk.consecutiveLossHalt) {
      const latestLoss = recent[0];
      const latestClosedAtMs = latestLoss ? parseDbTimeMs(latestLoss.closedAt ?? latestLoss.createdAt, this.now()) : this.now();
      this.consecutiveHaltUntilMs = latestClosedAtMs + this.consecutiveLossHaltMinutes() * 60_000;
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

function parseDbTimeMs(value: string, fallbackMs: number): number {
  const parsed = Date.parse(value.includes('T') ? value : `${value.replace(' ', 'T')}Z`);
  return Number.isFinite(parsed) ? parsed : fallbackMs;
}
