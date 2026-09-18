import { VersionedTransaction } from '@solana/web3.js';

/**
 * Pre-graduation execution via PumpPortal trade-local (S3).
 *
 * DECISION (S3a, sim-proven): instruction bytes come from trade-local, never
 * hand-rolled. Sim archaeology showed why: the deployed buy has evolved to an
 * 18-account layout (verified piecemeal — discriminator 66063d1201daebea,
 * fee-recipient offset 41, global/user volume accumulators, fee_config,
 * fee_program, creator_vault) that tracks program upgrades. Hand-rolling that
 * surface is how money is lost to rot; a maintained builder plus OUR gates
 * (simulate-first via the Broadcaster, payer/mint verification here, existing
 * breakers) is the safe composition:
 *
 *   trade-local builds → verifyTradeTx checks shape → Broadcaster.broadcast
 *   simulates (refuses to send on sim error) → sends → confirms.
 *
 * Custody never leaves the bot: trade-local returns UNSIGNED bytes; signing
 * uses the existing executor signer. No secret is ever sent anywhere.
 */

const TRADE_LOCAL_URL = 'https://pumpportal.fun/api/trade-local';

export type CurveTradeSide = 'buy' | 'sell';

export interface CurveTradeRequest {
  /** Base58 wallet address — also becomes the transaction fee payer. */
  wallet: string;
  mint: string;
  side: CurveTradeSide;
  /** Buy size in SOL (side === 'buy'). */
  amountSol?: number;
  /** Sell size in base units (side === 'sell'). */
  tokenAmount?: bigint;
  slippagePct: number;
  priorityFeeSol: number;
}

export interface CurveTradeTx {
  txBytes: Uint8Array;
  side: CurveTradeSide;
  mint: string;
}

export class CurveTradeError extends Error {}

/** Request unsigned buy/sell bytes from the maintained builder. No signing, no send. */
export async function fetchCurveTradeTx(
  req: CurveTradeRequest,
  fetchImpl: typeof fetch = fetch,
): Promise<CurveTradeTx> {
  if (req.side === 'buy' && !(req.amountSol !== undefined && req.amountSol > 0)) {
    throw new CurveTradeError('buy requires amountSol > 0');
  }
  if (req.side === 'sell' && !(req.tokenAmount !== undefined && req.tokenAmount > 0n)) {
    throw new CurveTradeError('sell requires tokenAmount > 0');
  }
  const body: Record<string, unknown> = {
    publicKey: req.wallet,
    action: req.side,
    mint: req.mint,
    slippage: req.slippagePct,
    priorityFee: req.priorityFeeSol,
    pool: 'pump',
  };
  if (req.side === 'buy') {
    body['denominatedInSol'] = 'true';
    body['amount'] = req.amountSol;
  } else {
    body['denominatedInSol'] = 'false';
    body['amount'] = req.tokenAmount!.toString();
  }
  let res: Response;
  try {
    res = await fetchImpl(TRADE_LOCAL_URL, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(body),
    });
  } catch (err) {
    throw new CurveTradeError(`trade-local unreachable: ${String(err)}`);
  }
  if (!res.ok) {
    const text = await res.text().catch(() => '');
    throw new CurveTradeError(`trade-local HTTP ${res.status}: ${text.slice(0, 200)}`);
  }
  const txBytes = new Uint8Array(await res.arrayBuffer());
  if (txBytes.length === 0) throw new CurveTradeError('trade-local returned empty body');
  return { txBytes, side: req.side, mint: req.mint };
}

export interface TradeVerifyResult {
  ok: boolean;
  reason?: string;
}

/**
 * Shape checks BEFORE signing: deserializable versioned tx, fee payer is our
 * wallet, the mint is referenced in static keys, at least one program
 * invocation exists. Economic validity (price, slippage) is enforced by the
 * Broadcaster's simulation gate, not here.
 */
export function verifyCurveTradeTx(
  tx: CurveTradeTx,
  wallet: string,
): TradeVerifyResult {
  let vtx: VersionedTransaction;
  try {
    vtx = VersionedTransaction.deserialize(tx.txBytes);
  } catch {
    return { ok: false, reason: 'unparseable-transaction' };
  }
  const staticKeys = vtx.message.staticAccountKeys.map((k) => k.toBase58());
  if (staticKeys.length === 0) return { ok: false, reason: 'no-accounts' };
  if (staticKeys[0] !== wallet) return { ok: false, reason: 'payer-mismatch' };
  if (!staticKeys.includes(tx.mint)) return { ok: false, reason: 'mint-missing' };
  if (vtx.message.compiledInstructions.length === 0) return { ok: false, reason: 'no-instructions' };
  return { ok: true };
}
