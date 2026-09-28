import { PublicKey, type AccountInfo } from '@solana/web3.js';
import BN from 'bn.js';
import { GLOBAL_CONFIG_PDA, PUMP_AMM_FEE_CONFIG_PDA, PUMP_AMM_SDK, type SwapSolanaState } from '@pump-fun/pump-swap-sdk';
import type { RpcClient } from '../core/rpc.ts';
import { PROGRAM_IDS } from '../core/constants.ts';
import { decodeMint } from '../enrichment/mint.ts';
import { logger } from '../core/logger.ts';

/**
 * Offline PumpSwap swap state (fast path). The SDK's `swapSolanaState` makes
 * three SERIAL round trips per buy: global + fee config + pool, then mints +
 * vaults, then the user's ATAs. None of that needs to be on the hot path:
 *
 *  - global / fee config change on governance timescales → AmmConfigCache,
 *    refreshed in the background;
 *  - pool, base mint, vaults and the user's ATAs → the ONE batched read the
 *    fast screen already makes (guardrails/fastRead.ts), which parks the
 *    finished state here for the executor to pick up.
 *
 * Quoting and instruction building stay inside the SDK (`buyQuoteInput`), so
 * its fee model and remaining-accounts logic are never re-implemented.
 */

/** A base64 account read, as RpcClient returns it. */
export interface RawAccountRead {
  data: string;
  owner: string;
  lamports: number;
  executable?: boolean;
}

export function toAccountInfo(a: RawAccountRead): AccountInfo<Buffer> {
  return {
    data: Buffer.from(a.data, 'base64'),
    owner: new PublicKey(a.owner),
    lamports: a.lamports,
    executable: a.executable ?? false,
    rentEpoch: 0,
  };
}

export interface AmmConfigs {
  globalConfig: SwapSolanaState['globalConfig'];
  feeConfig: SwapSolanaState['feeConfig'];
}

/**
 * PumpSwap GlobalConfig + FeeConfig, kept warm off the hot path. A stale
 * config is bounded by `refreshMs`; the worst case is a buy the program
 * rejects (fee recipient rotated), which costs the fee, never funds.
 */
export class AmmConfigCache {
  private readonly rpc: Pick<RpcClient, 'getMultipleAccountsBase64'>;
  private readonly refreshMs: number;
  private readonly log = logger.child({ mod: 'amm-config' });
  private value: AmmConfigs | null = null;
  private timer: NodeJS.Timeout | null = null;

  constructor(rpc: Pick<RpcClient, 'getMultipleAccountsBase64'>, refreshMs = 30_000) {
    this.rpc = rpc;
    this.refreshMs = refreshMs;
  }

  /** Synchronous: null until the first refresh lands. */
  get(): AmmConfigs | null {
    return this.value;
  }

  async refresh(): Promise<void> {
    try {
      const [global, fee] = await this.rpc.getMultipleAccountsBase64(
        [GLOBAL_CONFIG_PDA.toBase58(), PUMP_AMM_FEE_CONFIG_PDA.toBase58()],
        'confirmed',
      );
      if (!global) throw new Error('PumpSwap global config not found');
      this.value = {
        globalConfig: PUMP_AMM_SDK.decodeGlobalConfig(toAccountInfo(global)),
        feeConfig: fee ? PUMP_AMM_SDK.decodeFeeConfig(toAccountInfo(fee)) : null,
      };
    } catch (err) {
      // Keep serving the previous value; the buy falls back to the SDK's
      // online read when there has never been one.
      this.log.warn('amm config refresh failed', { err, cached: this.value !== null });
    }
  }

  start(): void {
    if (this.timer) return;
    void this.refresh();
    this.timer = setInterval(() => void this.refresh(), this.refreshMs);
    this.timer.unref?.();
  }

  stop(): void {
    if (this.timer) clearInterval(this.timer);
    this.timer = null;
  }
}

/** Everything the fast read fetched that a buy state needs. */
export interface SwapStateParts {
  poolKey: string;
  pool: RawAccountRead;
  baseMint: RawAccountRead;
  baseReserve: bigint;
  quoteReserveLamports: bigint;
  user: string;
  userBaseAta: string;
  userBaseAccount: RawAccountRead | null;
  userQuoteAta: string;
  userQuoteAccount: RawAccountRead | null;
}

/** Assemble the SDK's SwapSolanaState from pre-read accounts — no RPC. */
export function buildSwapState(parts: SwapStateParts, configs: AmmConfigs): SwapSolanaState {
  const poolAccountInfo = toAccountInfo(parts.pool);
  const pool = PUMP_AMM_SDK.decodePool(poolAccountInfo);
  const mint = decodeMint(parts.baseMint.data, parts.baseMint.owner);
  // The SDK reads only `.supply` (fee tiers by market cap); the rest mirrors
  // spl-token's RawMint so the type is honest.
  const baseMintAccount: SwapSolanaState['baseMintAccount'] = {
    mintAuthorityOption: mint.mintAuthority ? 1 : 0,
    mintAuthority: mint.mintAuthority ? new PublicKey(mint.mintAuthority) : PublicKey.default,
    supply: mint.supply,
    decimals: mint.decimals,
    isInitialized: true,
    freezeAuthorityOption: mint.freezeAuthority ? 1 : 0,
    freezeAuthority: mint.freezeAuthority ? new PublicKey(mint.freezeAuthority) : PublicKey.default,
  };
  return {
    globalConfig: configs.globalConfig,
    feeConfig: configs.feeConfig,
    poolKey: new PublicKey(parts.poolKey),
    poolAccountInfo,
    pool,
    poolBaseAmount: new BN(parts.baseReserve.toString()),
    poolQuoteAmount: new BN(parts.quoteReserveLamports.toString()),
    baseTokenProgram: new PublicKey(parts.baseMint.owner),
    quoteTokenProgram: new PublicKey(PROGRAM_IDS.TOKEN),
    baseMint: pool.baseMint,
    baseMintAccount,
    user: new PublicKey(parts.user),
    userBaseTokenAccount: new PublicKey(parts.userBaseAta),
    userQuoteTokenAccount: new PublicKey(parts.userQuoteAta),
    userBaseAccountInfo: parts.userBaseAccount ? toAccountInfo(parts.userBaseAccount) : null,
    userQuoteAccountInfo: parts.userQuoteAccount ? toAccountInfo(parts.userQuoteAccount) : null,
  };
}

const MAX_ENTRIES = 64;

/**
 * Swap states the fast screen prefetched, keyed by pool, for the executor's
 * buy. Short TTL: past it the reserves are too old to quote from and the
 * executor does its own online read, exactly as before.
 */
export class PrefetchedSwapStates {
  private readonly ttlMs: number;
  private readonly now: () => number;
  private readonly entries = new Map<string, { state: SwapSolanaState; atMs: number }>();

  constructor(ttlMs = 3_000, now: () => number = Date.now) {
    this.ttlMs = ttlMs;
    this.now = now;
  }

  put(state: SwapSolanaState): void {
    const key = state.poolKey.toBase58();
    this.entries.delete(key);
    this.entries.set(key, { state, atMs: this.now() });
    while (this.entries.size > MAX_ENTRIES) {
      const oldest = this.entries.keys().next().value;
      if (oldest === undefined) break;
      this.entries.delete(oldest);
    }
  }

  /** The fresh state for this pool and user, else null. */
  get(poolKey: string, user: PublicKey): SwapSolanaState | null {
    const hit = this.entries.get(poolKey);
    if (!hit) return null;
    if (this.now() - hit.atMs > this.ttlMs) {
      this.entries.delete(poolKey);
      return null;
    }
    return hit.state.user.equals(user) ? hit.state : null;
  }
}
