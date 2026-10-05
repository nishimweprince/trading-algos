import { PublicKey } from '@solana/web3.js';
import { canonicalPumpPoolPda } from '@pump-fun/pump-swap-sdk';
import type { RpcClient } from '../core/rpc.ts';
import type { GraduationEvent } from '../core/types.ts';
import { logger } from '../core/logger.ts';
import { PROGRAM_IDS, WSOL_MINT, LAMPORTS_PER_SOL } from '../core/constants.ts';
import { deriveAta } from '../core/ata.ts';
import { decodePool, decodeTokenAccountAmount, type PoolInfo } from '../enrichment/pool.ts';
import { decodeMint, type MintInfo } from '../enrichment/mint.ts';
import { buildSwapState, type AmmConfigCache, type PrefetchedSwapStates, type RawAccountRead } from '../executor/swapState.ts';
import type { PreArmer } from './preArm.ts';

/**
 * Fast screen read (detection → verdict in one round trip).
 *
 * Every address the verdict and the buy need is derivable from the mint, so
 * they go out as ONE getMultipleAccounts at `processed`:
 *
 *   canonical pool PDA · base mint · quote vault · base vault (SPL + 2022)
 *   · the wallet's WSOL + base ATAs (SPL + 2022, dry-run/live)
 *
 * Both token-program variants are requested because the mint's program is
 * only known once the mint comes back; the unused one costs nothing.
 *
 * The pool lives at pump.fun's canonical PDA, which only the pump.fun
 * program's pool-authority can create — so a decoded pool there IS the
 * provenance proof (engine P0). Reserves come from the vaults; when a vault
 * read lags the migration (< 1 SOL), the migrate tx's own post balances are
 * used instead of waiting a slot to re-read.
 */

export interface FastSnapshot {
  pool: PoolInfo;
  /** Null when the mint account did not come back (P0 fails). */
  mintInfo: MintInfo | null;
  readMs: number;
  attempts: number;
  reservesFrom: 'rpc' | 'tx' | 'rpc_reread';
  /** A buy state was built and parked for the executor. */
  swapStatePrefetched: boolean;
  /** Served from the curve-completion pre-arm (pool pushed by LaserStream): no RPC read. */
  preArmed: boolean;
  /** Served by the zero-derivation gate (everything precomputed at completion): no RPC, no PDA work. */
  gated: boolean;
}

export type FastReadResult =
  | { ok: true; snapshot: FastSnapshot }
  | { ok: false; reason: 'pool_not_found' | 'not_pumpswap_pool'; detail: string; readMs: number; attempts: number };

export interface FastReaderDeps {
  rpc: Pick<RpcClient, 'getMultipleAccountsBase64'>;
  /** ms to wait before each attempt; one attempt per entry. */
  retryDelaysMs: readonly number[];
  /** Trading wallet (dry-run/live): its ATAs join the batch and a buy state is prefetched. */
  user?: string;
  swapStates?: PrefetchedSwapStates;
  ammConfigs?: AmmConfigCache;
  /** Curve-completion pre-arm; when it holds this mint's pool, the read is skipped. */
  preArmer?: Pick<PreArmer, 'peek' | 'waitForPool' | 'release' | 'noteNoArm' | 'onPrefetched'>;
  /** How long a pending pool push may still beat the RPC read (pre-arm race). */
  poolRaceMs?: number;
  sleep?: (ms: number) => Promise<void>;
  now?: () => number;
}

/** Below this, a quote vault read is presumed to predate the migration deposit. */
const LAGGING_VAULT_LAMPORTS = BigInt(LAMPORTS_PER_SOL);
const VAULT_REREAD_DELAY_MS = 400;
/** Precomputed screens live as long as an arm (60 s); the gate also requires a live peek. */
const PRECOMPUTED_TTL_MS = 60_000;
const PRECOMPUTED_CAP = 1024;

/**
 * Everything about a mint knowable at curve completion: decoded mint plus
 * every address the screen needs. Built off the hot path when the prefetch
 * lands; the detection-time gate only verifies the pushed pool + tx reserves.
 */
interface Precomputed {
  mintInfo: MintInfo;
  poolKey: string;
  baseVaultSpl: string;
  baseVault22: string;
  quoteVault: string;
  atMs: number;
}

export class FastPoolReader {
  private readonly deps: FastReaderDeps;
  private readonly sleep: (ms: number) => Promise<void>;
  private readonly now: () => number;
  private readonly userWsolAta: string | undefined;
  private readonly precomputed = new Map<string, Precomputed>();
  private readonly log = logger.child({ mod: 'fast-read' });

  constructor(deps: FastReaderDeps) {
    this.deps = deps;
    this.sleep = deps.sleep ?? ((ms) => new Promise((resolve) => setTimeout(resolve, ms)));
    this.now = deps.now ?? Date.now;
    this.userWsolAta = deps.user ? deriveAta(deps.user, WSOL_MINT, false) : undefined;
    deps.preArmer?.onPrefetched((mint) => this.precompute(mint));
  }

  /**
   * Completion-time precompute (off the hot path): decode the prefetched mint
   * and derive the vault addresses from the armed pool key. Anything missing
   * or undecodable deletes the entry — the gate then simply does not fire.
   */
  private precompute(mint: string): void {
    const armed = this.deps.preArmer?.peek(mint);
    if (!armed?.mintAccount) {
      this.precomputed.delete(mint);
      this.log.debug('gate precompute skipped', { mint, reason: 'no-mint-account' });
      return;
    }
    let mintInfo: MintInfo | null;
    try {
      mintInfo = decodeMint(armed.mintAccount.data, armed.mintAccount.owner);
    } catch (err) {
      this.precomputed.delete(mint);
      this.log.debug('gate precompute skipped', { mint, reason: 'mint-undecodable', err });
      return;
    }
    if (!mintInfo) {
      this.precomputed.delete(mint);
      this.log.debug('gate precompute skipped', { mint, reason: 'mint-undecodable' });
      return;
    }
    this.precomputed.set(mint, {
      mintInfo,
      poolKey: armed.poolKey,
      baseVaultSpl: deriveAta(armed.poolKey, mint, false),
      baseVault22: deriveAta(armed.poolKey, mint, true),
      quoteVault: deriveAta(armed.poolKey, WSOL_MINT, false),
      atMs: this.now(),
    });
    if (this.precomputed.size > PRECOMPUTED_CAP) {
      for (const [k, v] of this.precomputed) {
        if (this.now() - v.atMs > PRECOMPUTED_TTL_MS) this.precomputed.delete(k);
      }
      while (this.precomputed.size > PRECOMPUTED_CAP) {
        const oldest = this.precomputed.keys().next().value;
        if (oldest === undefined) break;
        this.precomputed.delete(oldest);
      }
    }
  }

  /**
   * Detection-time gate: when the mint was precomputed at completion, the
   * pool was pushed, and the migrate tx carries balances, assemble the exact
   * snapshot the pushed path would build — zero RPC, zero derivation. Any
   * precondition failure returns null and the normal path runs. Sync by
   * construction: nothing here may await.
   */
  private readGated(g: GraduationEvent): FastReadResult | null {
    const started = this.now();
    const mint = g.mint;
    const miss = (reason: string): null => {
      this.log.debug('gate fall-through', { mint, reason });
      return null;
    };
    const pre = this.precomputed.get(mint);
    if (!pre) return miss('no-precompute');
    if (started - pre.atMs > PRECOMPUTED_TTL_MS) return miss('stale-precompute');
    const armed = this.deps.preArmer?.peek(mint);
    if (!armed?.pool) return miss('no-push');
    if (armed.poolKey !== pre.poolKey) return miss('poolkey-mismatch');
    const tx = reservesFromTx(g, pre.poolKey, mint);
    if (!tx) return miss('no-tx-balances');
    if (tx.quote < LAGGING_VAULT_LAMPORTS) return miss('thin-tx');
    const decoded = decodePool(armed.pool.data, armed.pool.owner, pre.poolKey);
    if (!decoded || decoded.baseMint !== mint) return miss('pool-undecodable');
    const is2022 = pre.mintInfo.isToken2022;
    if (decoded.baseVault !== (is2022 ? pre.baseVault22 : pre.baseVaultSpl) || decoded.quoteVault !== pre.quoteVault) {
      return miss('vault-mismatch'); // Non-derived vaults: the full path re-reads them.
    }
    // No user (paper mode): the swap prefetch no-ops downstream; the screen values are unaffected.
    const userBaseAta = armed.addrs ? (is2022 ? armed.addrs.userBase22 : armed.addrs.userBaseSpl) : '';
    const snapshot: FastSnapshot = {
      pool: {
        ...decoded,
        isCanonical: decoded.coinCreator !== PROGRAM_IDS.SYSTEM,
        baseReserve: tx.base,
        quoteReserveLamports: tx.quote,
      },
      mintInfo: { ...pre.mintInfo, extensions: [...pre.mintInfo.extensions] },
      readMs: 0,
      attempts: 0,
      reservesFrom: 'tx',
      swapStatePrefetched: false,
      preArmed: true,
      gated: true,
    };
    snapshot.swapStatePrefetched = this.prefetchSwapState({
      poolKey: pre.poolKey,
      pool: armed.pool,
      baseMint: armed.mintAccount,
      baseReserve: tx.base,
      quoteReserveLamports: tx.quote,
      userBaseAta,
      userBaseAccount: armed.accounts.get(userBaseAta) ?? null,
      userQuoteAccount: armed.addrs ? (armed.accounts.get(armed.addrs.userWsol) ?? null) : null,
    });
    snapshot.readMs = this.now() - started;
    return { ok: true, snapshot };
  }

  async read(g: GraduationEvent): Promise<FastReadResult> {
    const started = this.now();
    const mint = g.mint;
    const gated = this.readGated(g);
    if (gated) {
      this.deps.preArmer?.release(mint, 'hit');
      return gated;
    }
    const poolKey = canonicalPumpPoolPda(new PublicKey(mint)).toBase58();
    const armed = this.deps.preArmer?.peek(mint) ?? null;
    const user = this.deps.user;

    const keys = {
      pool: poolKey,
      mint,
      quoteVault: deriveAta(poolKey, WSOL_MINT, false),
      baseVaultSpl: deriveAta(poolKey, mint, false),
      baseVault22: deriveAta(poolKey, mint, true),
      ...(user && this.userWsolAta
        ? { userWsol: this.userWsolAta, userBaseSpl: deriveAta(user, mint, false), userBase22: deriveAta(user, mint, true) }
        : {}),
    };
    const names = Object.keys(keys) as Array<keyof typeof keys>;
    const addresses = names.map((n) => keys[n]!);

    let accts: Record<string, RawAccountRead | null> = {};
    let attempts = 0;
    const rpcRead = async (): Promise<Record<string, RawAccountRead | null>> => {
      let got: Record<string, RawAccountRead | null> = {};
      for (const delayMs of this.deps.retryDelaysMs) {
        if (delayMs > 0) await this.sleep(delayMs);
        attempts++;
        const res = await this.deps.rpc.getMultipleAccountsBase64(addresses, 'processed').catch(() => null);
        if (!res) continue;
        got = Object.fromEntries(names.map((n, i) => [n, res[i] ?? null]));
        if (got.pool) break;
      }
      return got;
    };
    // Armed but pool not pushed yet: only the pool account is missing (mint +
    // user ATAs were prefetched, reserves come from the migrate tx) — fetch
    // just it, on the same retry schedule as the full read.
    const poolRead = async (): Promise<RawAccountRead | null> => {
      for (const delayMs of this.deps.retryDelaysMs) {
        if (delayMs > 0) await this.sleep(delayMs);
        attempts++;
        const res = await this.deps.rpc.getMultipleAccountsBase64([poolKey], 'processed').catch(() => null);
        if (res?.[0]) return res[0];
      }
      return null;
    };
    // Pre-armed: the pool is pushed by LaserStream and the rest was prefetched
    // at curve completion — no round trip. Vaults are not prefetched (they do
    // not exist before the migrate tx); reserves come from its post balances.
    // Not pushed yet: race the push against a single-account pool read — the
    // full batch is pointless when everything else is already prefetched.
    const usable = Boolean(armed && armed.mintAccount && armed.poolKey === poolKey);
    let preArmed = false;
    let poolAcct: RawAccountRead | null = null;
    if (usable && armed?.pool) {
      preArmed = true;
    } else if (usable && armed && this.deps.preArmer) {
      const readP = poolRead();
      const pushed = await Promise.race([
        readP.then(() => false),
        this.deps.preArmer.waitForPool(mint, this.deps.poolRaceMs ?? 1_000),
      ]);
      if (pushed && armed.pool) preArmed = true;
      else poolAcct = await readP;
    } else {
      accts = await rpcRead();
    }
    if (armed && (preArmed || poolAcct)) {
      const pool = preArmed ? armed.pool : poolAcct;
      const byAddr = (addr: string): RawAccountRead | null =>
        addr === armed.poolKey ? pool : addr === mint ? armed.mintAccount : (armed.accounts.get(addr) ?? null);
      accts = Object.fromEntries(names.map((n) => [n, byAddr(keys[n]!)]));
    }
    if (armed) this.deps.preArmer?.release(mint, preArmed ? 'hit' : usable ? 'push-missed' : 'no-prefetch');
    else this.deps.preArmer?.noteNoArm();
    const readMs = () => this.now() - started;
    if (!accts.pool) {
      return { ok: false, reason: 'pool_not_found', detail: `no account at canonical pool ${short(poolKey)} after ${attempts} reads`, readMs: readMs(), attempts };
    }
    const decoded = decodePool(accts.pool.data, accts.pool.owner, poolKey);
    if (!decoded || decoded.baseMint !== mint) {
      return { ok: false, reason: 'not_pumpswap_pool', detail: 'canonical PDA does not hold a PumpSwap WSOL pool for this mint', readMs: readMs(), attempts };
    }

    const mintAcct = accts.mint ?? null;
    let mintInfo: MintInfo | null = null;
    try {
      mintInfo = mintAcct ? decodeMint(mintAcct.data, mintAcct.owner) : null;
    } catch {
      mintInfo = null;
    }
    const is2022 = mintInfo?.isToken2022 ?? false;

    // Vaults: the pool's own ATAs (the SDK derives them the same way). A
    // mismatch means an unexpected layout — read the recorded ones instead.
    let baseAcct = is2022 ? accts.baseVault22 : accts.baseVaultSpl;
    let quoteAcct = accts.quoteVault;
    const derivedBase = is2022 ? keys.baseVault22 : keys.baseVaultSpl;
    if (decoded.baseVault !== derivedBase || decoded.quoteVault !== keys.quoteVault) {
      const [b, q] = await this.deps.rpc.getMultipleAccountsBase64([decoded.baseVault, decoded.quoteVault], 'processed');
      baseAcct = b ?? null;
      quoteAcct = q ?? null;
    }
    let baseReserve = baseAcct ? decodeTokenAccountAmount(baseAcct.data) : 0n;
    let quoteReserveLamports = quoteAcct ? decodeTokenAccountAmount(quoteAcct.data) : 0n;
    let reservesFrom: FastSnapshot['reservesFrom'] = 'rpc';
    if (quoteReserveLamports < LAGGING_VAULT_LAMPORTS) {
      const tx = reservesFromTx(g, poolKey, mint);
      if (tx && tx.quote >= LAGGING_VAULT_LAMPORTS) {
        baseReserve = tx.base;
        quoteReserveLamports = tx.quote;
        reservesFrom = 'tx';
      } else if (!tx) {
        // No tx balances to fall back on. Vaults read-and-lagging get one
        // re-read a slot later (rare). Armed paths never read the vaults, so
        // read them now — and only wait out a slot when that first sighting
        // lags too (same batch can straddle the migration).
        const vaultsUnread = preArmed || poolAcct !== null;
        if (!vaultsUnread) await this.sleep(VAULT_REREAD_DELAY_MS);
        let [b, q] = await this.deps.rpc.getMultipleAccountsBase64([decoded.baseVault, decoded.quoteVault], 'processed');
        if (vaultsUnread && (!q || decodeTokenAccountAmount(q.data) < LAGGING_VAULT_LAMPORTS)) {
          await this.sleep(VAULT_REREAD_DELAY_MS);
          [b, q] = await this.deps.rpc.getMultipleAccountsBase64([decoded.baseVault, decoded.quoteVault], 'processed');
        }
        if (b) baseReserve = decodeTokenAccountAmount(b.data);
        if (q) quoteReserveLamports = decodeTokenAccountAmount(q.data);
        reservesFrom = 'rpc_reread';
      }
    }

    const pool: PoolInfo = {
      ...decoded,
      isCanonical: decoded.coinCreator !== PROGRAM_IDS.SYSTEM,
      baseReserve,
      quoteReserveLamports,
    };

    const snapshot: FastSnapshot = { pool, mintInfo, readMs: 0, attempts, reservesFrom, swapStatePrefetched: false, preArmed, gated: false };
    snapshot.swapStatePrefetched = this.prefetchSwapState({
      poolKey,
      pool: accts.pool,
      baseMint: mintAcct,
      baseReserve,
      quoteReserveLamports,
      userBaseAta: is2022 ? keys.userBase22! : keys.userBaseSpl!,
      userBaseAccount: (is2022 ? accts.userBase22 : accts.userBaseSpl) ?? null,
      userQuoteAccount: accts.userWsol ?? null,
    });
    snapshot.readMs = readMs();
    return { ok: true, snapshot };
  }

  /**
   * Park a buy state for the executor from already-in-hand accounts (shared
   * by the gated and normal paths). False when there is no wallet, no
   * configs, no mint — or the SDK decoder rejects the layout, in which case
   * the executor's online read still works, just slower.
   */
  private prefetchSwapState(input: {
    poolKey: string;
    pool: RawAccountRead | null;
    baseMint: RawAccountRead | null;
    baseReserve: bigint;
    quoteReserveLamports: bigint;
    userBaseAta: string;
    userBaseAccount: RawAccountRead | null;
    userQuoteAccount: RawAccountRead | null;
  }): boolean {
    const user = this.deps.user;
    const configs = this.deps.ammConfigs?.get();
    const { pool, baseMint } = input;
    if (!user || !configs || !this.deps.swapStates || !baseMint || !pool || !this.userWsolAta) return false;
    try {
      this.deps.swapStates.put(
        buildSwapState(
          {
            poolKey: input.poolKey,
            pool,
            baseMint,
            baseReserve: input.baseReserve,
            quoteReserveLamports: input.quoteReserveLamports,
            user,
            userBaseAta: input.userBaseAta,
            userBaseAccount: input.userBaseAccount,
            userQuoteAta: this.userWsolAta,
            userQuoteAccount: input.userQuoteAccount,
          },
          configs,
        ),
      );
      return true;
    } catch {
      return false;
    }
  }
}

/** Pool vault balances from the migrate tx's post balances (owner = the pool). */
export function reservesFromTx(g: GraduationEvent, poolKey: string, mint: string): { base: bigint; quote: bigint } | null {
  const own = g.txBalances?.filter((b) => b.owner === poolKey);
  const quote = own?.find((b) => b.mint === WSOL_MINT)?.amount;
  const base = own?.find((b) => b.mint === mint)?.amount;
  return quote !== undefined && base !== undefined ? { base, quote } : null;
}

function short(a: string): string {
  return `${a.slice(0, 4)}…${a.slice(-4)}`;
}
