import type { Executor } from '../executor/index.ts';
import { combineTradeActuals, type FillActuals, type TradeActuals } from '../executor/fillActuals.ts';
import type { Repositories } from '../persistence/repositories.ts';
import { logger } from '../core/logger.ts';

export type ActualsExecutor = Pick<Executor, 'fillActuals'>;

export interface BookTradeArgs {
  mint: string;
  baseIsToken2022?: boolean | undefined;
  /** The buy. Required unless `entryOptional` (e.g. an orphan whose cost was already booked). */
  entryTx?: string | null | undefined;
  /** Every exit tx sent for this position, landed or not. */
  exitTxs: Iterable<string | null | undefined>;
  /** Book with entry = 0 when there is no entry tx (cost booked elsewhere). */
  entryOptional?: boolean;
  /** Target row; defaults to the mint's latest CLOSED row. */
  rowid?: number;
  /**
   * Record the buy's ATA rent lock as a wallet event (default true). The
   * backfill passes false: its rent was locked before the reconciliation
   * anchor, and an event stamped "now" would skew it.
   */
  recordRent?: boolean;
}

/**
 * Books wallet-true PnL onto a CLOSED positions row: SOL actually spent on the
 * buy and actually received from every exit tx, from the chain's balances.
 * Off the hot path — called after finalize, by recovery, by the orphan
 * reconciler, and by the backfill CLI.
 */
export class ActualsRecorder {
  private readonly executor: ActualsExecutor;
  private readonly repos: Repositories;
  private readonly log = logger.child({ mod: 'actuals' });

  constructor(deps: { executor: ActualsExecutor; repos: Repositories }) {
    this.executor = deps.executor;
    this.repos = deps.repos;
  }

  /** Never rejects: callers fire-and-forget it after a close. */
  async bookTrade(args: BookTradeArgs, opts: { attempts?: number; delayMs?: number } = {}): Promise<TradeActuals | null> {
    try {
      return await this.book(args, opts);
    } catch (err) {
      this.log.warn('booking trade actuals failed — modelled pnl kept', { mint: args.mint, err });
      return null;
    }
  }

  private async book(args: BookTradeArgs, opts: { attempts?: number; delayMs?: number }): Promise<TradeActuals | null> {
    const t22 = args.baseIsToken2022 ?? false;
    const exitSigs = [...new Set([...args.exitTxs].filter((s): s is string => typeof s === 'string' && s.length > 0))];
    const entry = args.entryTx ? await this.executor.fillActuals(args.entryTx, args.mint, t22, opts) : null;
    if (!entry && !args.entryOptional) {
      this.log.warn('entry tx not readable — leaving modelled pnl', { mint: args.mint, entryTx: args.entryTx });
      return null;
    }
    // Exits that never landed are simply not on chain (null): nothing paid.
    const exits: Array<FillActuals | null> = [];
    for (const sig of exitSigs) {
      exits.push(await this.executor.fillActuals(sig, args.mint, t22, { attempts: opts.attempts ?? 3, delayMs: opts.delayMs ?? 1_000 }));
    }
    const actuals = combineTradeActuals(entry ?? ZERO_ENTRY, exits);
    if (!actuals) return null;
    try {
      this.repos.applyPositionActuals(args.mint, actuals, args.rowid);
      if (entry && entry.rentLamports > 0 && args.recordRent !== false) {
        this.repos.recordWalletEvent({ kind: 'rent_lock', lamports: -entry.rentLamports, mint: args.mint, signature: entry.signature });
      }
    } catch (err) {
      this.log.error('failed to persist trade actuals', { mint: args.mint, err });
      return null;
    }
    this.log.info('trade actuals booked', {
      mint: args.mint,
      entrySol: round(actuals.entrySol),
      exitSol: round(actuals.exitSol),
      feesSol: round(actuals.feesSol),
      walletPnlSol: round(actuals.walletPnlSol),
    });
    return actuals;
  }
}

const ZERO_ENTRY: FillActuals = {
  signature: '',
  slot: 0,
  walletLamportsDelta: 0,
  feeLamports: 0,
  rentLamports: 0,
  tokenRawDelta: 0n,
  err: null,
};

function round(n: number): number {
  return Number(n.toFixed(6));
}
