import { PublicKey } from '@solana/web3.js';
import { canonicalPumpPoolPda } from '@pump-fun/pump-swap-sdk';
import type { RpcClient } from '../core/rpc.ts';
import type { GraduationEvent } from '../core/types.ts';
import { PROGRAM_IDS, WSOL_MINT, LAMPORTS_PER_SOL } from '../core/constants.ts';
import { deriveAta } from '../core/ata.ts';
import { decodePool, decodeTokenAccountAmount, type PoolInfo } from '../enrichment/pool.ts';
import { decodeMint, type MintInfo } from '../enrichment/mint.ts';
import { buildSwapState, type AmmConfigCache, type PrefetchedSwapStates, type RawAccountRead } from '../executor/swapState.ts';

/**
 * Fast screen read (detection → verdict in one round trip).
 *
 * Every address the verdict and the buy need is derivable from the mint, so
 * they go out as ONE getMultipleAccounts at `processed`:
 *
 *   canonical pool PDA · base mint · quote vault · base vault (SPL + 2022)
 *   · creator ATA (SPL + 2022, when the launch feed named the creator)
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
  /** Creator share of supply, when the launch feed's creator IS the pool's coin_creator. */
  creatorHolding?: { creator: string; share: number };
  readMs: number;
  attempts: number;
  reservesFrom: 'rpc' | 'tx' | 'rpc_reread';
  /** A buy state was built and parked for the executor. */
  swapStatePrefetched: boolean;
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
  /** Creator named by the launch feed, if the mint's creation was seen. */
  launchCreator?: (mint: string) => string | null;
  sleep?: (ms: number) => Promise<void>;
  now?: () => number;
}

/** Below this, a quote vault read is presumed to predate the migration deposit. */
const LAGGING_VAULT_LAMPORTS = BigInt(LAMPORTS_PER_SOL);
const VAULT_REREAD_DELAY_MS = 400;

export class FastPoolReader {
  private readonly deps: FastReaderDeps;
  private readonly sleep: (ms: number) => Promise<void>;
  private readonly now: () => number;
  private readonly userWsolAta: string | undefined;

  constructor(deps: FastReaderDeps) {
    this.deps = deps;
    this.sleep = deps.sleep ?? ((ms) => new Promise((resolve) => setTimeout(resolve, ms)));
    this.now = deps.now ?? Date.now;
    this.userWsolAta = deps.user ? deriveAta(deps.user, WSOL_MINT, false) : undefined;
  }

  async read(g: GraduationEvent): Promise<FastReadResult> {
    const started = this.now();
    const mint = g.mint;
    const poolKey = canonicalPumpPoolPda(new PublicKey(mint)).toBase58();
    const creator = this.deps.launchCreator?.(mint) ?? null;
    const user = this.deps.user;

    const keys = {
      pool: poolKey,
      mint,
      quoteVault: deriveAta(poolKey, WSOL_MINT, false),
      baseVaultSpl: deriveAta(poolKey, mint, false),
      baseVault22: deriveAta(poolKey, mint, true),
      ...(creator ? { creatorSpl: deriveAta(creator, mint, false), creator22: deriveAta(creator, mint, true) } : {}),
      ...(user && this.userWsolAta
        ? { userWsol: this.userWsolAta, userBaseSpl: deriveAta(user, mint, false), userBase22: deriveAta(user, mint, true) }
        : {}),
    };
    const names = Object.keys(keys) as Array<keyof typeof keys>;
    const addresses = names.map((n) => keys[n]!);

    let accts: Record<string, RawAccountRead | null> = {};
    let attempts = 0;
    for (const delayMs of this.deps.retryDelaysMs) {
      if (delayMs > 0) await this.sleep(delayMs);
      attempts++;
      const res = await this.deps.rpc.getMultipleAccountsBase64(addresses, 'processed').catch(() => null);
      if (!res) continue;
      accts = Object.fromEntries(names.map((n, i) => [n, res[i] ?? null]));
      if (accts.pool) break;
    }
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
        // No tx balances to fall back on: one re-read a slot later (rare).
        await this.sleep(VAULT_REREAD_DELAY_MS);
        const [b, q] = await this.deps.rpc.getMultipleAccountsBase64([decoded.baseVault, decoded.quoteVault], 'processed');
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

    const snapshot: FastSnapshot = { pool, mintInfo, readMs: 0, attempts, reservesFrom, swapStatePrefetched: false };
    if (creator && mintInfo && mintInfo.supply > 0n && creator === decoded.coinCreator) {
      const ata = is2022 ? accts.creator22 : accts.creatorSpl;
      const held = ata ? decodeTokenAccountAmount(ata.data) : 0n;
      snapshot.creatorHolding = { creator, share: Number(held) / Number(mintInfo.supply) };
    }

    const configs = this.deps.ammConfigs?.get();
    if (user && configs && this.deps.swapStates && mintAcct && this.userWsolAta) {
      try {
        this.deps.swapStates.put(
          buildSwapState(
            {
              poolKey,
              pool: accts.pool,
              baseMint: mintAcct,
              baseReserve,
              quoteReserveLamports,
              user,
              userBaseAta: is2022 ? keys.userBase22! : keys.userBaseSpl!,
              userBaseAccount: (is2022 ? accts.userBase22 : accts.userBaseSpl) ?? null,
              userQuoteAta: this.userWsolAta,
              userQuoteAccount: accts.userWsol ?? null,
            },
            configs,
          ),
        );
        snapshot.swapStatePrefetched = true;
      } catch {
        // Unexpected pool layout for the SDK decoder: the executor's online
        // read still works, it is just a slower buy.
      }
    }
    snapshot.readMs = readMs();
    return { ok: true, snapshot };
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
