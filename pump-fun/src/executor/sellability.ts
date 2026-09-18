import {
  Connection,
  PublicKey,
  SystemProgram,
  TransactionInstruction,
  VersionedTransaction,
  type AddressLookupTableAccount,
} from '@solana/web3.js';
import type { Config } from '../config/schema.ts';
import { PROGRAM_IDS } from '../core/constants.ts';
import { Wallet } from './wallet.ts';
import { PumpAmmClient } from './pumpAmm.ts';
import { isExceededSlippage } from './slippage.ts';
import { withTimeout } from './timeout.ts';
import { assembleSignedSwapTx } from './assemble.ts';
import { logger } from '../core/logger.ts';
import { createFailoverFetch } from '../core/rpc.ts';

/**
 * H4 sellability / honeypot probe (Section 6.1). Builds an ATOMIC buy-then-sell
 * in a single transaction and simulates it: the buy provides the tokens, the
 * sell in the same tx proves they can be sold. If the sell leg is blocked
 * (honeypot / transfer trap) the simulation fails.
 *
 * Requires a FUNDED wallet — the buy leg spends SOL in simulation, so an
 * unfunded/ephemeral wallet yields an account/funds error which we classify as
 * `unknown` (not `fail`), never a false honeypot verdict. On canonical PumpSwap
 * pools the static checks (H2 freeze, H9 transfer-fee/hook/non-transferable)
 * already cover the on-chain honeypot vectors; this adds a dynamic backstop.
 */

export type SellabilityStatus = 'pass' | 'fail' | 'unknown';
export type SellabilityReason =
  | 'tx_too_large'
  | 'buy_only_ok'
  | 'account_setup_unavailable'
  | 'wallet_unfunded'
  | 'rpc_unavailable'
  /**
   * Pump AMM ExceededSlippage (6004): the pool moved past the probe's bound
   * between quote and simulate — it is being sniped right now. Says nothing
   * about sellability, so it is `unknown`, and it is deliberately NOT one of
   * the reasons the guardrail engine may tolerate: entering behind a spike
   * was the 3–15 s stop pattern on 2026-09-16.
   */
  | 'price_moved'
  /**
   * The buy ix itself failed for a reason other than slippage. Proves nothing
   * about the sell leg, so `unknown`, and never tolerated (a buy that cannot
   * land is not an entry either).
   */
  | 'buy_failed'
  | 'sell_failed'
  | 'not_run';

export interface SellabilityResult {
  status: SellabilityStatus;
  detail: string;
  reason?: SellabilityReason;
  txBytes?: number;
  usedLookupTable?: boolean;
  /**
   * Pool quote-reserve move (%) between the enrichment snapshot and the probe's
   * own state read — the early sniping the probe used to veto implicitly via its
   * 15% bound. Recorded so the operator can gate it explicitly
   * (guardrails.maxProbeMovePct) and so it lands in enrichment_json.
   */
  poolMovePct?: number;
}

/**
 * Where the PumpSwap swap ixs sit in the simulated transaction, so an
 * InstructionError can be attributed to a leg. Indices are into the final tx
 * (after the compute-budget ixs assembleSignedSwapTx prepends).
 */
export interface ProbeLayout {
  buyIx: number;
  sellIx: number;
}

/**
 * Outcome of assembling + simulating one probe. `simErr === null` is a clean
 * simulation; a non-null `simErr` is an on-chain program error; `assembleErr`
 * is a build/transport failure (e.g. the 1232-byte overflow).
 */
type ProbeRun =
  | { txBytes: number; simErr: unknown | null }
  | { assembleErr: unknown; txBytes?: number };

export const PROBE_SOL = 0.02;
/**
 * Fallback probe bound when config does not set guardrails.sellabilityProbeSlippagePct.
 * The bound only decides whether the simulation gets AS FAR AS the sell leg — a
 * honeypot fails the sell at any bound — so it is deliberately wide: at 15% the
 * buy ix hit ExceededSlippage on ~95% of real graduations (2026-09-18 review)
 * and H4 learned nothing about sellability.
 */
export const DEFAULT_PROBE_SLIPPAGE_PCT = 50;
const ATOMIC_CU_LIMIT = 600_000;
/** assembleSignedSwapTx prepends setComputeUnitLimit + setComputeUnitPrice. */
const COMPUTE_BUDGET_IX_COUNT = 2;
const PUMP_SWAP_PROGRAM = PROGRAM_IDS.PUMP_SWAP;
const ATA_PROGRAM = new PublicKey(PROGRAM_IDS.ASSOCIATED_TOKEN);

export function createIdempotentAtaInstruction(
  payer: PublicKey,
  owner: PublicKey,
  mint: PublicKey,
  tokenProgram: PublicKey,
): { address: PublicKey; instruction: TransactionInstruction } {
  const address = PublicKey.findProgramAddressSync(
    [owner.toBuffer(), tokenProgram.toBuffer(), mint.toBuffer()],
    ATA_PROGRAM,
  )[0];
  return {
    address,
    instruction: new TransactionInstruction({
      programId: ATA_PROGRAM,
      keys: [
        { pubkey: payer, isSigner: true, isWritable: true },
        { pubkey: address, isSigner: false, isWritable: true },
        { pubkey: owner, isSigner: false, isWritable: false },
        { pubkey: mint, isSigner: false, isWritable: false },
        { pubkey: SystemProgram.programId, isSigner: false, isWritable: false },
        { pubkey: tokenProgram, isSigner: false, isWritable: false },
      ],
      data: Buffer.from([1]), // CreateIdempotent
    }),
  };
}

/**
 * Flatten any thrown value to searchable text.
 *
 * JSON.stringify is NOT enough on its own: an Error's `name`/`message`/`stack`
 * are non-enumerable, so `JSON.stringify(new Error('boom'))` is literally `{}`.
 * isTxTooLarge used to match against that, which meant the 1232-byte overflow —
 * the dominant H4 unknown cause — was never recognised when it arrived as a real
 * Error, and fell through to `rpc_unavailable`. That silently disabled both
 * guardrails.tolerateTxTooLargeSellability and guardrails.sellabilityBuyOnlyBackstop,
 * because each is gated on reason === 'tx_too_large'.
 */
function errorSearchText(err: unknown): string {
  const parts: string[] = [];
  const visit = (e: unknown, depth: number): void => {
    if (e == null || depth > 3) return;
    if (e instanceof Error) {
      parts.push(e.name, e.message);
      if (e.stack) parts.push(e.stack);
      visit((e as { cause?: unknown }).cause, depth + 1);
      return;
    }
    if (typeof e === 'string') { parts.push(e); return; }
    try { parts.push(JSON.stringify(e)); } catch { parts.push(String(e)); }
  };
  visit(err, 0);
  return parts.join(' ');
}

function isTxTooLarge(err: unknown): boolean {
  return /encoding overruns Uint8Array|VersionedTransaction too large|transaction.*too large/i.test(
    errorSearchText(err),
  );
}

/** `[index, …]` of an on-chain InstructionError anywhere in the thrown value. */
export function instructionErrorIndex(err: unknown): number | undefined {
  const m = /"InstructionError":\s*\[\s*(\d+)/.exec(errorSearchText(err));
  return m ? Number(m[1]) : undefined;
}

/**
 * Normalize probe failures so policy can distinguish risk from infrastructure.
 *
 * With a `layout`, an InstructionError is attributed by position: anything
 * before the buy ix is account setup (the SDK's optional `extendAccount` /
 * ATA creates — an Anchor constraint error there was being read as
 * `sell_failed`, i.e. a honeypot verdict on a healthy pool); the buy ix is
 * `price_moved` (6004) or `buy_failed`; only the sell ix onward is
 * `sell_failed`. Without a layout the older text-based rules apply.
 */
export function classifySellabilityError(
  err: unknown,
  source: 'simulation' | 'transport' = 'simulation',
  layout?: ProbeLayout,
): SellabilityReason {
  const s = errorSearchText(err);
  if (isTxTooLarge(err)) return 'tx_too_large';
  if (layout && source === 'simulation') {
    const idx = instructionErrorIndex(err);
    if (idx !== undefined) {
      if (idx < layout.buyIx) return 'account_setup_unavailable';
      if (idx < layout.sellIx) return isExceededSlippage(err) ? 'price_moved' : 'buy_failed';
      return 'sell_failed';
    }
  }
  if (isExceededSlippage(err)) return 'price_moved';
  if (/InsufficientFunds|insufficient (?:lamports|funds)|debit an account|attempt to debit/i.test(s)) {
    return 'wallet_unfunded';
  }
  if (/AccountNotFound|could not find account|invalid account data|account .* does not exist/i.test(s)) {
    return 'account_setup_unavailable';
  }
  if (source === 'transport' || /429|timed? out|fetch failed|network|ECONN|socket|service unavailable/i.test(s)) {
    return 'rpc_unavailable';
  }
  return 'sell_failed';
}

/** Anchor discriminators of the PumpSwap swap ixs (sha256("global:buy"/"global:sell")[0..8]). */
const BUY_DISCRIMINATOR = '66063d1201daebea';
const SELL_DISCRIMINATOR = '33e685a4017f83ad';

/**
 * Locate the PumpSwap buy and sell ixs in the probe's instruction list. The
 * SDK emits a variable prefix (optional `extendAccount` — itself a PumpSwap
 * ix — WSOL/base ATA creates, transfer, syncNative), so the swap ixs are found
 * by discriminator, not by program id or position. Indices are into the final
 * tx (compute-budget ixs first).
 */
export function probeLayout(ixs: readonly TransactionInstruction[]): ProbeLayout | undefined {
  let buy = -1;
  let sell = -1;
  ixs.forEach((ix, i) => {
    if (ix.programId.toBase58() !== PUMP_SWAP_PROGRAM) return;
    const disc = Buffer.from(ix.data.subarray(0, 8)).toString('hex');
    if (disc === BUY_DISCRIMINATOR && buy === -1) buy = i;
    else if (disc === SELL_DISCRIMINATOR && sell === -1) sell = i;
  });
  if (buy === -1 || sell === -1 || sell < buy) return undefined;
  return { buyIx: buy + COMPUTE_BUDGET_IX_COUNT, sellIx: sell + COMPUTE_BUDGET_IX_COUNT };
}

function fmtPct(x: number | undefined): string {
  return x === undefined ? '?' : `${x >= 0 ? '+' : ''}${x.toFixed(1)}%`;
}

export class SellabilitySimulator {
  private readonly connection: Connection;
  private readonly wallet: Wallet;
  private readonly pumpAmm: PumpAmmClient;
  private readonly lookupTableAddress: string | undefined;
  private readonly buyOnlyBackstop: boolean;
  private readonly getCachedBalanceLamports: (() => bigint | null) | undefined;
  private readonly commitment: 'processed' | 'confirmed';
  private readonly simulateTimeoutMs: number;
  private readonly probeSlippagePct: number;
  private lookupTable: AddressLookupTableAccount | null | undefined;
  private readonly log = logger.child({ mod: 'sellability' });

  constructor(deps: {
    httpUrl: string;
    /**
     * Independent read endpoints tried when the primary stalls or rate-limits.
     * Without this the probe used a single hardcoded Connection: a stalled
     * Helius endpoint made every H4 check `unknown` regardless of
     * rpc.fallbackHttp, since RpcClient's failover never covered this path.
     */
    fallbackHttpUrls?: readonly string[];
    config: Config;
    /** In-memory wallet cache — skip getBalance on the probe hot path when set. */
    getCachedBalanceLamports?: () => bigint | null;
  }) {
    this.commitment = deps.config.execution.stateCommitment;
    this.simulateTimeoutMs = deps.config.execution.simulateTimeoutMs;
    const urls = [deps.httpUrl, ...(deps.fallbackHttpUrls ?? [])];
    const readTimeoutMs = deps.config.rpc?.readTimeoutMs ?? 900;
    this.connection =
      urls.length > 1
        ? new Connection(deps.httpUrl, {
            commitment: this.commitment,
            fetch: createFailoverFetch(urls, { timeoutMs: readTimeoutMs }),
          })
        : new Connection(deps.httpUrl, this.commitment);
    this.wallet = Wallet.load(deps.config.wallet.keypairEnvVar, deps.config.mode);
    this.pumpAmm = new PumpAmmClient(deps.httpUrl, this.commitment, {
      fallbackHttpUrls: urls.slice(1),
      timeoutMs: readTimeoutMs,
    });
    this.lookupTableAddress = deps.config.guardrails.sellabilityLookupTableAddress;
    this.buyOnlyBackstop = deps.config.guardrails.sellabilityBuyOnlyBackstop;
    this.probeSlippagePct = deps.config.guardrails.sellabilityProbeSlippagePct ?? DEFAULT_PROBE_SLIPPAGE_PCT;
    this.getCachedBalanceLamports = deps.getCachedBalanceLamports;
  }

  async check(
    poolAddress: string,
    baseReserve: bigint,
    quoteReserveLamports: bigint,
    baseMint: string,
    baseIsToken2022 = false,
  ): Promise<SellabilityResult> {
    if (this.wallet.ephemeral) {
      return { status: 'unknown', reason: 'wallet_unfunded', detail: 'no persistent wallet for sell simulation' };
    }
    const probeLamports = BigInt(Math.floor(PROBE_SOL * 1e9));
    if (baseReserve <= 0n || quoteReserveLamports <= 0n) {
      return { status: 'unknown', reason: 'account_setup_unavailable', detail: 'pool reserves unavailable' };
    }

    let usedLookupTable = false;
    let txBytes: number | undefined;
    let poolMovePct: number | undefined;
    const withMove = <T extends object>(r: T): T & { poolMovePct?: number } =>
      poolMovePct !== undefined ? { ...r, poolMovePct } : r;
    try {
      const user = this.wallet.keypair.publicKey;
      // Prefer the in-memory wallet cache so H4 does not add a getBalance RTT
      // on the graduation → send path. Fall back to RPC only if never primed.
      let balance: number;
      const cached = this.getCachedBalanceLamports?.();
      if (cached != null) {
        balance = Number(cached);
      } else {
        try {
          balance = await this.connection.getBalance(user, 'confirmed');
        } catch (err) {
          return { status: 'unknown', reason: 'rpc_unavailable', detail: `wallet preflight failed: ${(err as Error).message}` };
        }
      }
      const requiredLamports = Number(probeLamports) + 5_000_000;
      if (balance < requiredLamports) {
        return {
          status: 'unknown',
          reason: 'wallet_unfunded',
          detail: `wallet balance ${balance} below probe requirement ${requiredLamports} lamports`,
        };
      }
      // One state read for both legs; the sell moves exactly the buy's
      // base_amount_out (see PumpAmmClient.buildProbeSwap).
      const swap = await this.pumpAmm.buildProbeSwap(poolAddress, user, probeLamports, this.probeSlippagePct);
      const { buyIxs, sellIxs } = swap;
      if (swap.base <= 0n) {
        return { status: 'unknown', reason: 'account_setup_unavailable', detail: 'probe too small for reserves' };
      }
      poolMovePct = (Number(swap.stateQuoteReserveLamports) / Number(quoteReserveLamports) - 1) * 100;
      const tokenProgram = new PublicKey(baseIsToken2022 ? PROGRAM_IDS.TOKEN_2022 : PROGRAM_IDS.TOKEN);
      const ataSetup = createIdempotentAtaInstruction(user, user, new PublicKey(baseMint), tokenProgram);
      let ataExists: boolean;
      try {
        ataExists = Boolean(await this.connection.getAccountInfo(ataSetup.address, this.commitment));
      } catch (err) {
        return { status: 'unknown', reason: 'rpc_unavailable', detail: `ATA preflight failed: ${(err as Error).message}` };
      }
      const sdkCreatesAta = buyIxs.some((ix) => ix.programId.equals(ATA_PROGRAM));
      const setupIxs = !ataExists && !sdkCreatesAta ? [ataSetup.instruction] : [];
      const lookupTable = await this.getLookupTable();
      usedLookupTable = Boolean(lookupTable);

      // Primary probe: atomic buy+sell in one tx proves sellability directly.
      const atomicIxs = [...setupIxs, ...buyIxs, ...sellIxs];
      const layout = probeLayout(atomicIxs);
      const atomic = await this.runProbe(atomicIxs, lookupTable);
      if (atomic.txBytes !== undefined) {
        txBytes = atomic.txBytes;
        this.log.debug('sellability tx assembled', { poolAddress, sellabilityTxBytes: txBytes, usedLookupTable, poolMovePct });
      }

      const atomicErr = 'assembleErr' in atomic ? atomic.assembleErr : atomic.simErr;
      if (atomicErr === null) {
        return withMove({
          status: 'pass',
          detail: `atomic buy+sell simulated cleanly (pool moved ${fmtPct(poolMovePct)} since enrichment)`,
          usedLookupTable,
          ...(txBytes !== undefined ? { txBytes } : {}),
        });
      }
      const atomicReason = classifySellabilityError(
        atomicErr,
        'assembleErr' in atomic ? 'transport' : 'simulation',
        layout,
      );

      // When the atomic probe overflows the 1232-byte tx limit — the dominant H4
      // "unknown" cause — fall back to simulating the buy leg alone. A clean buy
      // proves the pool is real, buyable, and the account setup lands; the
      // sell-block honeypot vectors are already covered on-chain by H2 (freeze)
      // and H9 (Token-2022 traps), which the guardrail engine requires to pass
      // before admitting this as a relaxed-risk accept.
      if (atomicReason === 'tx_too_large' && this.buyOnlyBackstop) {
        const buyOnly = await this.runProbe([...setupIxs, ...buyIxs], lookupTable);
        const buyErr = 'assembleErr' in buyOnly ? buyOnly.assembleErr : buyOnly.simErr;
        if (buyErr === null) {
          this.log.debug('sellability buy-leg backstop clean', {
            poolAddress,
            atomicTxBytes: txBytes,
            buyTxBytes: buyOnly.txBytes,
          });
          const backstopBytes = buyOnly.txBytes ?? txBytes;
          return withMove({
            status: 'unknown',
            reason: 'buy_only_ok',
            detail:
              `atomic probe too large (${txBytes ?? '?'}B); buy leg simulated cleanly — ` +
              `sell safety covered by H2/H9 static checks`,
            usedLookupTable,
            ...(backstopBytes !== undefined ? { txBytes: backstopBytes } : {}),
          });
        }
        // Buy leg also failed: a failing buy proves nothing about sellability, so
        // keep it inconclusive rather than a false honeypot verdict.
        const buyReason = classifySellabilityError(
          buyErr,
          'assembleErr' in buyOnly ? 'transport' : 'simulation',
          layout,
        );
        // Only a genuine account-setup problem is "inconclusive setup". A
        // slippage failure means the pool is moving; an RPC failure means we
        // could not look. Neither may be relabelled into a tolerated reason.
        const reason: SellabilityReason =
          buyReason === 'tx_too_large' ||
          buyReason === 'price_moved' ||
          buyReason === 'buy_failed' ||
          buyReason === 'rpc_unavailable'
            ? buyReason
            : 'account_setup_unavailable';
        return withMove({
          status: 'unknown',
          reason,
          detail: `atomic probe too large; buy-leg backstop inconclusive (${buyReason}): ${errText(buyErr)}`,
          usedLookupTable,
          ...(txBytes !== undefined ? { txBytes } : {}),
        });
      }

      if (atomicReason !== 'sell_failed') {
        this.log.debug('sellability probe inconclusive', { poolAddress, reason: atomicReason, poolMovePct, err: atomicErr });
        return withMove({
          status: 'unknown',
          reason: atomicReason,
          detail: `${atomicReason} (pool moved ${fmtPct(poolMovePct)} since enrichment): ${errText(atomicErr)}`,
          usedLookupTable,
          ...(txBytes !== undefined ? { txBytes } : {}),
        });
      }
      return withMove({
        status: 'fail',
        reason: 'sell_failed',
        detail: `sell leg failed: ${errText(atomicErr)}`,
        usedLookupTable,
        ...(txBytes !== undefined ? { txBytes } : {}),
      });
    } catch (err) {
      const reason = classifySellabilityError(err, 'transport');
      if (reason !== 'sell_failed') {
        return {
          status: 'unknown',
          reason,
          detail: `simulation error: ${(err as Error).message}`,
          ...(txBytes !== undefined ? { txBytes } : {}),
          usedLookupTable,
        };
      }
      return {
        status: 'fail',
        reason: 'sell_failed',
        detail: `simulation error: ${(err as Error).message}`,
        ...(txBytes !== undefined ? { txBytes } : {}),
        usedLookupTable,
      };
    }
  }

  /**
   * Assemble + simulate a single probe instruction list. Returns the simulated
   * error (null = clean) with the serialized byte count, or the assemble/transport
   * error when the tx can't even be built or sent (e.g. the 1232-byte overflow,
   * raised either by web3.js serialization or the RPC's own size reject).
   */
  private async runProbe(
    ixs: TransactionInstruction[],
    lookupTable: AddressLookupTableAccount | null,
  ): Promise<ProbeRun> {
    let bytes: Uint8Array;
    try {
      bytes = await assembleSignedSwapTx(ixs, {
        connection: this.connection,
        wallet: this.wallet,
        feePlan: { priorityMicroLamports: 50_000, jitoTipLamports: 0 },
        computeUnitLimit: ATOMIC_CU_LIMIT,
        ...(lookupTable ? { addressLookupTableAccounts: [lookupTable] } : {}),
      });
    } catch (err) {
      return { assembleErr: err };
    }
    const txBytes = bytes.length;
    try {
      const sim = await withTimeout(
        this.connection.simulateTransaction(VersionedTransaction.deserialize(bytes), {
          sigVerify: false,
          replaceRecentBlockhash: true,
          commitment: this.commitment,
        }),
        this.simulateTimeoutMs,
        'sellability simulate',
      );
      return { txBytes, simErr: sim.value.err ?? null };
    } catch (err) {
      return { assembleErr: err, txBytes };
    }
  }

  private async getLookupTable(): Promise<AddressLookupTableAccount | null> {
    if (!this.lookupTableAddress) return null;
    if (this.lookupTable !== undefined) return this.lookupTable;
    try {
      const res = await this.connection.getAddressLookupTable(new PublicKey(this.lookupTableAddress));
      this.lookupTable = res.value;
      if (!this.lookupTable) {
        this.log.warn('configured sellability lookup table not found', { lookupTable: this.lookupTableAddress });
      }
    } catch (err) {
      this.lookupTable = null;
      this.log.warn('failed to load sellability lookup table', { lookupTable: this.lookupTableAddress, err });
    }
    return this.lookupTable;
  }
}

function errText(err: unknown): string {
  if (err instanceof Error) return err.message;
  try {
    return JSON.stringify(err);
  } catch {
    return String(err);
  }
}
