import type { Config } from '../config/schema.ts';
import type { Repositories } from '../persistence/repositories.ts';
import type { RpcClient } from '../core/rpc.ts';
import type { Executor } from './index.ts';
import type { RiskManager } from '../risk/manager.ts';
import { logger } from '../core/logger.ts';
import { getActiveRunSession } from '../core/session.ts';
import { fetchBondingCurve, curvePriceSol, curveRealSol } from '../enrichment/curve.ts';
import { screenPregrad, curveCompletionPct } from '../guardrails/pregrad.ts';
import { fetchCurveTradeTx, verifyCurveTradeTx } from './curve.ts';

export type CurveExitReason =
  | 'TAKE_PROFIT'
  | 'STOP_LOSS'
  | 'TRAILING_STOP'
  | 'TIME_STOP'
  | 'GRADUATED_HOLD'
  | 'EMERGENCY_EXIT';

export interface CurveExitState {
  entryPrice: number;
  peakPrice: number;
  entryMs: number;
}

export interface CurveExitConfig {
  takeProfitPct: number;
  stopLossPct: number;
  /** Give-back from the peak that locks profit (arms once TP is touched). */
  trailPct: number;
  timeStopMs: number;
}

/**
 * Pure exit evaluator over curve mids. Trailing arms at takeProfitPct: once
 * the peak touches TP, a `trailPct` give-back exits. Returns the reason or
 * 'HOLD'. Peak tracking lives with the caller.
 */
export function evaluateCurveExit(
  st: CurveExitState,
  price: number,
  nowMs: number,
  cfg: CurveExitConfig,
): 'HOLD' | Exclude<CurveExitReason, 'GRADUATED_HOLD' | 'EMERGENCY_EXIT'> {
  if (!(price > 0 && st.entryPrice > 0)) return 'HOLD';
  const gainPct = (price / st.entryPrice - 1) * 100;
  if (gainPct >= cfg.takeProfitPct) return 'TAKE_PROFIT';
  if (gainPct <= -cfg.stopLossPct) return 'STOP_LOSS';
  const peakGainPct = (st.peakPrice / st.entryPrice - 1) * 100;
  if (peakGainPct >= cfg.takeProfitPct && gainPct <= peakGainPct - cfg.trailPct) return 'TRAILING_STOP';
  if (nowMs - st.entryMs >= cfg.timeStopMs) return 'TIME_STOP';
  return 'HOLD';
}

interface ManagedPeak {
  peak: number;
  trough: number;
}

export interface CurveTraderOptions {
  now?: () => number;
}

/**
 * Pre-graduation live trader (S3b). Separate venue, separate ledger
 * (`curve_positions`), same guarantees as the post-grad lane:
 *
 * - Entry: pregrad.enabled + global risk.canEnter + dedicated sublimit
 *   (realized + consecutive losses) + concurrency cap. Candidate = open paper
 *   track whose fresh curve read passes screenPregrad at/over the completion
 *   threshold. Buy via trade-local → verify → signAndBroadcast (sim-first;
 *   failed sim never sends) → confirm → balance-reconcile. Confirmed-paid
 *   with zero balance engages the global kill switch (ambiguous fill — the
 *   post-grad 8bFvxa rule, mirrored).
 * - Management: curve mids → MFE/MAE → TP/stop/trailing/time exits, sells
 *   sized to the reconciled balance.
 * - Graduation mid-hold (S4 exit-into-migration): the curve program rejects
 *   post-complete trades, so a graduated position sells its reconciled
 *   balance through the migrated PumpSwap pool (Executor.sellAndConfirm),
 *   with proceeds measured as wallet SOL delta for venue-honest PnL. Pool
 *   unknown or sell unconfirmed → GRADUATED_HOLD park (EXITING state,
 *   documented row) retried by recovery — never force-closed, never orphaned.
 * - Recovery: PENDING_ENTRY from a past boot → FAILED; OPEN/EXITING resume
 *   (EXITING retries its sell) before any new entry.
 */
export class CurveTrader {
  private readonly config: Config;
  private readonly rpc: RpcClient;
  private readonly repos: Repositories;
  private readonly executor: Executor;
  private readonly risk: RiskManager;
  private readonly now: () => number;
  private readonly log = logger.child({ mod: 'curve-trader' });
  private readonly peaks = new Map<string, ManagedPeak>();
  private selectTimer: NodeJS.Timeout | null = null;
  private manageTimer: NodeJS.Timeout | null = null;
  private recovered = false;

  constructor(deps: {
    config: Config;
    rpc: RpcClient;
    repos: Repositories;
    executor: Executor;
    risk: RiskManager;
    opts?: CurveTraderOptions;
  }) {
    this.config = deps.config;
    this.rpc = deps.rpc;
    this.repos = deps.repos;
    this.executor = deps.executor;
    this.risk = deps.risk;
    this.now = deps.opts?.now ?? (() => Date.now());
  }

  start(): void {
    if (this.selectTimer) return;
    const cfg = this.config.pregrad;
    this.selectTimer = setInterval(() => void this.selectOnce().catch((e) => this.log.error('select failed', { e })), cfg.selectPollMs);
    this.manageTimer = setInterval(() => void this.manageOnce().catch((e) => this.log.error('manage failed', { e })), cfg.managePollMs);
    this.selectTimer.unref?.();
    this.manageTimer?.unref?.();
  }

  stop(): void {
    if (this.selectTimer) clearInterval(this.selectTimer);
    if (this.manageTimer) clearInterval(this.manageTimer);
    this.selectTimer = this.manageTimer = null;
    this.peaks.clear();
  }

  private laneHalted(): string | null {
    const cfg = this.config.pregrad;
    if (!cfg.enabled) return 'disabled';
    if (this.risk.killed) return 'global-kill';
    const gate = this.risk.canEnter();
    if (!gate.ok) return `risk:${gate.reason ?? 'blocked'}`;
    const dayStart = new Date(this.now());
    dayStart.setUTCHours(0, 0, 0, 0);
    const iso = dayStart.toISOString().slice(0, 19).replace('T', ' ');
    if (this.repos.curveRealizedSince(iso) <= -cfg.maxDailyLossSol) return 'sublimit-daily-loss';
    if (this.repos.curveConsecutiveLosses(cfg.maxConsecutiveLosses) >= cfg.maxConsecutiveLosses) {
      return 'sublimit-consecutive-losses';
    }
    return null;
  }

  /** Entry scan. Public for tests; the timer calls it live. */
  async selectOnce(): Promise<void> {
    if (!this.recovered) {
      await this.recover();
      this.recovered = true;
    }
    const halted = this.laneHalted();
    if (halted) {
      this.log.debug('curve lane halted', { reason: halted });
      return;
    }
    const cfg = this.config.pregrad;
    if (this.repos.listCurvePositionsByState('OPEN').length + this.repos.listCurvePositionsByState('EXITING').length >= cfg.maxConcurrent) {
      return;
    }
    const candidates = this.repos.listEntryCandidateTracks(10);
    for (const mint of candidates) {
      if (this.repos.isGraduated(mint)) continue;
      const reserves = await fetchBondingCurve(this.rpc, mint).catch(() => null);
      if (!reserves || reserves.complete) continue;
      const price = curvePriceSol(reserves);
      if (price === null) continue;
      const mintIndexed = await this.isMintIndexed(mint);
      const verdict = screenPregrad(
        {
          mint,
          priced: true,
          complete: false,
          tokenTotalSupply: reserves.tokenTotalSupply,
          realTokenReserves: reserves.realTokenReserves,
          realSol: curveRealSol(reserves),
          top10Share: null,
          creatorShare: null,
          mintIndexed,
        },
        {
          minCompletionPct: cfg.minCompletionPct,
          minRealSol: cfg.minRealSol,
          maxTop10Pct: cfg.maxTop10Pct,
          maxCreatorPct: cfg.maxCreatorPct,
          allowUnindexed: cfg.allowUnindexed,
        },
      );
      if (verdict.verdict !== 'accept') continue;
      await this.enter(mint, verdict.relaxed);
      return; // one entry per scan; concurrency cap enforced above
    }
  }

  private async isMintIndexed(mint: string): Promise<boolean> {
    try {
      const acct = await this.rpc.getAccountInfoBase64(mint, 'processed');
      return acct !== null;
    } catch {
      return false;
    }
  }

  private async enter(mint: string, relaxed: boolean): Promise<void> {
    const cfg = this.config.pregrad;
    const session = getActiveRunSession();
    const sizeSol = cfg.buySol;
    this.risk.reserveSol(sizeSol);
    const rowid = this.repos.recordCurvePosition({
      mint,
      state: 'PENDING_ENTRY',
      sizeSol,
      relaxedRisk: relaxed,
      sessionId: session?.id ?? null,
      configHash: session?.configHash ?? null,
    });
    const fail = (detail: string) => {
      this.repos.updateCurvePositionState(rowid, 'FAILED', { execution_json: JSON.stringify({ detail }) });
      this.risk.releaseSol(sizeSol);
      this.log.warn('curve entry failed', { mint, detail });
    };
    try {
      const trade = await fetchCurveTradeTx({
        wallet: this.executor.publicKey,
        mint,
        side: 'buy',
        amountSol: sizeSol,
        slippagePct: cfg.slippagePct,
        priorityFeeSol: cfg.priorityFeeSol,
      });
      const check = verifyCurveTradeTx(trade, this.executor.publicKey);
      if (!check.ok) {
        fail(`verify:${check.reason}`);
        return;
      }
      const res = await this.executor.signAndBroadcastCurveTrade(trade.txBytes, `curve-buy:${mint.slice(0, 8)}`);
      if (!res.sent || !res.confirmed || !res.signature) {
        fail(`broadcast:sent=${res.sent},confirmed=${res.confirmed},simErr=${JSON.stringify(res.simErr)?.slice(0, 200)}`);
        return;
      }
      const isT22 = await this.isToken2022(mint);
      const balance = await this.executor.reconcileTokenBalance(mint, isT22);
      if (balance <= 0n) {
        // Confirmed-paid with zero balance: ambiguous fill. Mirror the
        // post-grad rule — halt everything, do not trade blind.
        this.repos.updateCurvePositionState(rowid, 'FAILED', {
          entry_tx: res.signature,
          execution_json: JSON.stringify({ detail: 'ambiguous-fill:confirmed-paid-zero-balance' }),
        });
        this.risk.releaseSol(sizeSol);
        this.risk.engageKillSwitch('internal', `curve ambiguous fill ${mint}`);
        return;
      }
      const entryPrice = sizeSol / Number(balance);
      this.repos.updateCurvePositionState(rowid, 'OPEN', {
        entry_tx: res.signature,
        entry_price: entryPrice,
        entry_base_amount: balance.toString(),
        is_token_2022: isT22 ? 1 : 0,
        opened_at: new Date(this.now()).toISOString().slice(0, 19).replace('T', ' '),
      });
      this.peaks.set(mint, { peak: entryPrice, trough: entryPrice });
      this.risk.releaseSol(sizeSol);
      this.risk.applyBalanceDeltaSol(-sizeSol);
      this.log.info('curve position open', { mint, entryPrice, balance: balance.toString() });
    } catch (err) {
      fail(`exception:${String(err).slice(0, 200)}`);
    }
  }

  /** Exit management. Public for tests; the timer calls it live. */
  async manageOnce(): Promise<void> {
    if (!this.config.pregrad.enabled) return;
    const opens = this.repos.listCurvePositionsByState('OPEN');
    if (opens.length === 0) return;
    const cfg = this.config.pregrad;
    for (const row of opens) {
      const mint = row['mint'] as string;
      const rowid = row['rowid'] as number;
      if (this.repos.isGraduated(mint)) {
        // S4 venue switch (exit-into-migration): the curve program rejects
        // post-complete trades, so the exit moves to the migrated PumpSwap
        // pool. Falls back to the documented GRADUATED_HOLD park when the
        // pool is unknown or the sell does not confirm — never orphaned.
        await this.exitGraduated(rowid, mint);
        continue;
      }
      const reserves = await fetchBondingCurve(this.rpc, mint).catch(() => null);
      if (!reserves || reserves.complete) {
        this.repos.updateCurvePositionState(rowid, 'EXITING', { exit_reason: 'GRADUATED_HOLD' });
        this.log.info('curve complete flag — parked for S4 switch', { mint });
        continue;
      }
      const price = curvePriceSol(reserves);
      if (price === null) continue;
      const entryPrice = row['entry_price'] as number;
      if (!(entryPrice > 0)) continue;
      const peak = this.peaks.get(mint) ?? { peak: entryPrice, trough: entryPrice };
      if (price > peak.peak) peak.peak = price;
      if (price < peak.trough) peak.trough = price;
      this.peaks.set(mint, peak);
      const mfe = (peak.peak / entryPrice - 1) * 100;
      const mae = (peak.trough / entryPrice - 1) * 100;
      this.repos.updateCurvePositionState(rowid, 'OPEN', { mfe_pct: mfe, mae_pct: mae });
      const signal = evaluateCurveExit(
        { entryPrice, peakPrice: peak.peak, entryMs: Date.parse((row['opened_at'] as string).replace(' ', 'T') + 'Z') },
        price,
        this.now(),
        { takeProfitPct: cfg.takeProfitPct, stopLossPct: cfg.stopLossPct, trailPct: cfg.trailPct, timeStopMs: cfg.timeStopMinutes * 60_000 },
      );
      if (signal !== 'HOLD') await this.exit(rowid, mint, signal, price, mfe, mae);
    }
  }

  private async exit(
    rowid: number,
    mint: string,
    reason: Exclude<CurveExitReason, 'GRADUATED_HOLD' | 'EMERGENCY_EXIT'>,
    lastPrice: number,
    mfe: number,
    mae: number,
  ): Promise<void> {
    const cfg = this.config.pregrad;
    this.repos.updateCurvePositionState(rowid, 'EXITING', { exit_reason: reason });
    try {
      const row = this.repos.listCurvePositionsByState('EXITING').find((r) => r['rowid'] === rowid);
      const isT22 = (row?.['is_token_2022'] as number) === 1;
      const sizeSol = row?.['size_sol'] as number;
      const balance = await this.executor.reconcileTokenBalance(mint, isT22);
      if (balance <= 0n) {
        this.closeEmpty(rowid, mint, reason, sizeSol, mfe, mae);
        return;
      }
      const trade = await fetchCurveTradeTx({
        wallet: this.executor.publicKey,
        mint,
        side: 'sell',
        tokenAmount: balance,
        slippagePct: cfg.slippagePct,
        priorityFeeSol: cfg.priorityFeeSol,
      });
      const check = verifyCurveTradeTx(trade, this.executor.publicKey);
      if (!check.ok) {
        this.log.error('curve exit verify failed — stays EXITING for recovery', { mint, reason: check.reason });
        return;
      }
      const res = await this.executor.signAndBroadcastCurveTrade(trade.txBytes, `curve-exit:${mint.slice(0, 8)}`);
      if (!res.sent || !res.confirmed || !res.signature) {
        this.log.error('curve exit not confirmed — stays EXITING for recovery', { mint, reason });
        return;
      }
      const exitPrice = lastPrice;
      const gross = exitPrice * Number(balance) - sizeSol;
      const fees = cfg.priorityFeeSol * 2 + 0.00001;
      this.repos.updateCurvePositionState(rowid, 'CLOSED', {
        exit_tx: res.signature,
        exit_venue: 'curve',
        exit_price: exitPrice,
        gross_pnl_sol: gross,
        fees_sol: fees,
        net_pnl_sol: gross - fees,
        mfe_pct: mfe,
        mae_pct: mae,
        hold_ms: this.now() - Date.parse(((row?.['opened_at'] as string) ?? '').replace(' ', 'T') + 'Z'),
        closed_at: new Date(this.now()).toISOString().slice(0, 19).replace('T', ' '),
      });
      this.peaks.delete(mint);
      this.log.info('curve position closed', { mint, reason, gross });
    } catch (err) {
      this.log.error('curve exit exception — stays EXITING for recovery', { mint, reason, err: String(err).slice(0, 200) });
    }
  }

  /**
   * S4 venue switch (exit-into-migration policy): a graduated position sells
   * its reconciled balance through the migrated PumpSwap pool via the shared
   * Executor.sellAndConfirm path (PumpSwap-SDK sell, sim-gated broadcast,
   * confirm-checked — no signature-as-fill). Proceeds are measured as wallet
   * SOL delta so PnL attribution is venue-honest. Pool unknown or sell
   * unconfirmed → documented GRADUATED_HOLD park for retry, never orphaned,
   * never in `positions`.
   */
  private async exitGraduated(rowid: number, mint: string): Promise<void> {
    const cfg = this.config.pregrad;
    const pool = this.repos.getGraduationPool(mint);
    if (!pool) {
      this.repos.updateCurvePositionState(rowid, 'EXITING', { exit_reason: 'GRADUATED_HOLD' });
      this.log.info('curve position graduated mid-hold — pool unknown, parked', { mint });
      return;
    }
    const row = [
      ...this.repos.listCurvePositionsByState('OPEN'),
      ...this.repos.listCurvePositionsByState('EXITING'),
    ].find((r) => r['rowid'] === rowid);
    const isT22 = (row?.['is_token_2022'] as number) === 1;
    const sizeSol = (row?.['size_sol'] as number) ?? 0;
    const entryPrice = (row?.['entry_price'] as number) ?? 0;
    const peak = this.peaks.get(mint);
    const mfe =
      peak !== undefined && entryPrice > 0
        ? (peak.peak / entryPrice - 1) * 100
        : ((row?.['mfe_pct'] as number) ?? 0);
    const mae =
      peak !== undefined && entryPrice > 0
        ? (peak.trough / entryPrice - 1) * 100
        : ((row?.['mae_pct'] as number) ?? 0);
    this.repos.updateCurvePositionState(rowid, 'EXITING', { exit_reason: 'GRADUATED_HOLD', mfe_pct: mfe, mae_pct: mae });
    try {
      const balance = await this.executor.reconcileTokenBalance(mint, isT22);
      if (balance <= 0n) {
        this.closeEmpty(rowid, mint, 'GRADUATED_HOLD', sizeSol, mfe, mae, 'pumpswap');
        return;
      }
      const before = await this.rpc.getBalance(this.executor.publicKey, 'confirmed').catch(() => null);
      const res = await this.executor.sellAndConfirm(pool, mint, balance, cfg.slippagePct);
      if (!res.sent || !res.confirmed || !res.signature) {
        this.log.error('pumpswap graduation exit not confirmed — stays EXITING for recovery', { mint });
        return;
      }
      const after = await this.rpc.getBalance(this.executor.publicKey, 'confirmed').catch(() => null);
      const fees = cfg.priorityFeeSol * 2 + 0.00001;
      let exitPrice: number | null = null;
      let gross: number | null = null;
      if (before !== null && after !== null) {
        const proceeds = Number(after - before) / 1e9; // net of tx fees
        gross = proceeds + fees;
        exitPrice = gross / Number(balance);
      }
      this.repos.updateCurvePositionState(rowid, 'CLOSED', {
        exit_tx: res.signature,
        exit_venue: 'pumpswap',
        exit_price: exitPrice,
        gross_pnl_sol: gross !== null ? gross - sizeSol : null,
        fees_sol: fees,
        net_pnl_sol: gross !== null ? gross - fees - sizeSol : null,
        mfe_pct: mfe,
        mae_pct: mae,
        hold_ms: this.now() - Date.parse(((row?.['opened_at'] as string) ?? '').replace(' ', 'T') + 'Z'),
        closed_at: new Date(this.now()).toISOString().slice(0, 19).replace('T', ' '),
        execution_json: JSON.stringify({
          detail: exitPrice === null ? 'pumpswap-exit:no-balance-delta' : 'pumpswap-exit:balance-delta',
          pool,
        }),
      });
      this.peaks.delete(mint);
      this.log.info('curve position closed via pumpswap after graduation', { mint, gross });
    } catch (err) {
      this.log.error('pumpswap graduation exit exception — stays EXITING for recovery', {
        mint,
        err: String(err).slice(0, 200),
      });
    }
  }

  private closeEmpty(
    rowid: number,
    mint: string,
    reason: string,
    sizeSol: number,
    mfe: number,
    mae: number,
    venue = 'curve',
  ): void {
    this.repos.updateCurvePositionState(rowid, 'CLOSED', {
      exit_reason: 'EMERGENCY_EXIT',
      exit_venue: venue,
      gross_pnl_sol: -sizeSol,
      fees_sol: 0,
      net_pnl_sol: -sizeSol,
      mfe_pct: mfe,
      mae_pct: mae,
      hold_ms: 0,
      execution_json: JSON.stringify({ detail: `empty-at-exit:intended=${reason}` }),
      closed_at: new Date(this.now()).toISOString().slice(0, 19).replace('T', ' '),
    });
    this.peaks.delete(mint);
  }

  private async isToken2022(mint: string): Promise<boolean> {
    try {
      const acct = await this.rpc.getAccountInfoBase64(mint, 'processed');
      return (acct as { owner?: string } | null) !== null && (acct as { owner: string }).owner === 'TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb';
    } catch {
      return false;
    }
  }

  /** Crash recovery: settle past-boot rows before any new entry. */
  private async recover(): Promise<void> {
    for (const row of this.repos.listCurvePositionsByState('PENDING_ENTRY')) {
      this.repos.updateCurvePositionState(row['rowid'] as number, 'FAILED', {
        execution_json: JSON.stringify({ detail: 'pending-at-boot:never-sent-or-unknown' }),
      });
    }
    for (const row of this.repos.listCurvePositionsByState('EXITING')) {
      const reason = row['exit_reason'] as string;
      if (reason === 'GRADUATED_HOLD') {
        // S4: retry the PumpSwap venue-switch exit (pool may have arrived).
        await this.exitGraduated(row['rowid'] as number, row['mint'] as string);
        continue;
      }
      const mint = row['mint'] as string;
      this.log.info('curve recovery: retrying exit', { mint, reason });
      const mfe = (row['mfe_pct'] as number) ?? 0;
      const mae = (row['mae_pct'] as number) ?? 0;
      const last = (row['entry_price'] as number) ?? 0;
      await this.exit(row['rowid'] as number, mint, (reason || 'EMERGENCY_EXIT') as Exclude<CurveExitReason, 'GRADUATED_HOLD' | 'EMERGENCY_EXIT'>, last, mfe, mae);
    }
    const opens = this.repos.listCurvePositionsByState('OPEN').length;
    if (opens > 0) this.log.info('curve recovery: resuming open positions', { opens });
  }
}
