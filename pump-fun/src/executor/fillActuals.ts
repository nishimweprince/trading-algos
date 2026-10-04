import type { ParsedTokenBalance, ParsedTx, RpcClient } from '../core/rpc.ts';

/**
 * What a transaction actually did to the trading wallet, read from the
 * chain's own pre/post balances — the only numbers that agree with the wallet.
 *
 * The ledger used to price live trades from the intended size and the tick
 * that triggered the exit, with fees from the paper model, so a wallet that
 * made money could show a loss on the dashboard (and the other way round).
 */
export interface FillActuals {
  signature: string;
  slot: number;
  /** Signed change of the wallet's SOL (fee payer, index 0), lamports. Includes fee, tip, WSOL wrap/unwrap and rent. */
  walletLamportsDelta: number;
  /** Network fee (base + priority) the wallet paid, lamports. */
  feeLamports: number;
  /**
   * Lamports moved into (positive) or out of (negative) the wallet's token
   * account for this mint: rent locked when the ATA is created by the buy.
   * Refundable — the sweeper reclaims it — so it is kept out of trade PnL.
   */
  rentLamports: number;
  /** Signed change of the wallet's raw token balance for the mint. */
  tokenRawDelta: bigint;
  /** On-chain error (a failed tx still pays its fee). */
  err: unknown;
}

export function parseFillActuals(tx: ParsedTx, wallet: string, mint: string, tokenAccount?: string): FillActuals | null {
  if (!tx.meta) return null;
  const keys = tx.transaction.message.accountKeys.map((k) => (typeof k === 'string' ? k : k.pubkey));
  const walletIdx = keys.indexOf(wallet);
  if (walletIdx < 0) return null;
  const walletLamportsDelta = (tx.meta.postBalances[walletIdx] ?? 0) - (tx.meta.preBalances[walletIdx] ?? 0);

  let rentLamports = 0;
  if (tokenAccount) {
    const ataIdx = keys.indexOf(tokenAccount);
    if (ataIdx >= 0) {
      const pre = tx.meta.preBalances[ataIdx] ?? 0;
      const post = tx.meta.postBalances[ataIdx] ?? 0;
      // Only creation (0 -> rent) or close (rent -> 0) counts as rent movement.
      if (pre === 0 && post > 0) rentLamports = post;
      else if (pre > 0 && post === 0) rentLamports = -pre;
    }
  }

  const sumFor = (list: ParsedTokenBalance[] | undefined) =>
    (list ?? [])
      .filter((b) => b.mint === mint && (b.owner === wallet || (tokenAccount !== undefined && keys[b.accountIndex] === tokenAccount)))
      .reduce((acc, b) => acc + BigInt(b.uiTokenAmount.amount), 0n);
  const tokenRawDelta = sumFor(tx.meta.postTokenBalances) - sumFor(tx.meta.preTokenBalances);

  return {
    signature: tx.transaction.signatures[0] ?? '',
    slot: tx.slot,
    walletLamportsDelta,
    feeLamports: (tx.meta as { fee?: number }).fee ?? 0,
    rentLamports,
    tokenRawDelta,
    err: tx.meta.err ?? null,
  };
}

/**
 * Wallet-true trade totals from the entry tx and every exit tx (including
 * failed attempts, whose fee the wallet still paid). Rent is excluded from
 * PnL and reported separately so reconciliation can account for it.
 */
export interface TradeActuals {
  entrySol: number;
  exitSol: number;
  feesSol: number;
  walletPnlSol: number;
  rentLockedLamports: number;
}

export function combineTradeActuals(entry: FillActuals | null, exits: Array<FillActuals | null>): TradeActuals | null {
  if (!entry) return null;
  const toSol = (l: number) => l / 1e9;
  // entry spend = -(delta) minus rent locked into the new ATA (refundable)
  const entryLamports = -entry.walletLamportsDelta - Math.max(0, entry.rentLamports);
  let exitLamports = 0;
  let fees = entry.feeLamports;
  for (const x of exits) {
    if (!x) continue;
    // a close-in-tx would hand rent back: keep it out of proceeds too
    exitLamports += x.walletLamportsDelta + Math.min(0, x.rentLamports);
    fees += x.feeLamports;
  }
  return {
    entrySol: toSol(entryLamports),
    exitSol: toSol(exitLamports),
    feesSol: toSol(fees),
    walletPnlSol: toSol(exitLamports - entryLamports),
    rentLockedLamports: Math.max(0, entry.rentLamports),
  };
}

/**
 * Poll getTransaction until `signature` is indexed, then parse it. Null if it
 * never shows up (the tx never landed — nothing was paid).
 */
export async function fetchFillActuals(
  rpc: Pick<RpcClient, 'getParsedTransaction'>,
  signature: string,
  wallet: string,
  mint: string,
  tokenAccount: string,
  opts: { attempts?: number; delayMs?: number } = {},
): Promise<FillActuals | null> {
  const attempts = opts.attempts ?? 8;
  const delayMs = opts.delayMs ?? 1_500;
  for (let i = 0; i < attempts; i++) {
    try {
      const tx = await rpc.getParsedTransaction(signature, 'confirmed');
      if (tx) return parseFillActuals(tx, wallet, mint, tokenAccount);
    } catch {
      // transient RPC error: retry
    }
    if (i < attempts - 1) await new Promise((r) => setTimeout(r, delayMs));
  }
  return null;
}
