import { PublicKey } from '@solana/web3.js';
import { canonicalPumpPoolPda } from '@pump-fun/pump-swap-sdk';
import { base58Encode } from '../core/base58.ts';
import { PROGRAM_IDS, WSOL_MINT, LAMPORTS_PER_SOL } from '../core/constants.ts';
import type { RpcClient } from '../core/rpc.ts';

/**
 * PumpSwap (`pAMMBay…`) Pool account decoder. Field offsets are from the
 * official pump-amm IDL and were VERIFIED against live pools before use
 * (base_mint@43 matched the token, quote_mint@75 matched WSOL, vaults returned
 * sane reserves). Layout after the 8-byte Anchor discriminator:
 *
 *   8    pool_bump  u8
 *   9    index      u16
 *   11   creator    Pubkey            (pool creator / migration authority)
 *   43   base_mint  Pubkey
 *   75   quote_mint Pubkey            (== WSOL for graduation pools)
 *   107  lp_mint    Pubkey
 *   139  pool_base_token_account  Pubkey   (base reserve vault)
 *   171  pool_quote_token_account Pubkey   (SOL reserve vault)
 *   203  lp_supply  u64               (internal accounting field, NOT circulating LP)
 *   211  coin_creator Pubkey          (dev wallet; default == non-canonical)
 */

const OFF = {
  pool_bump: 8,
  index: 9,
  creator: 11,
  base_mint: 43,
  quote_mint: 75,
  lp_mint: 107,
  base_vault: 139,
  quote_vault: 171,
  lp_supply: 203,
  coin_creator: 211,
} as const;

const POOL_DISCRIMINATOR = 'f19a6d0411b16dbc';
const DEFAULT_PUBKEY = '11111111111111111111111111111111';
const INCINERATOR = '1nc1nerator11111111111111111111111111111111';

export interface PoolInfo {
  poolAddress: string;
  baseMint: string;
  quoteMint: string;
  lpMint: string;
  baseVault: string;
  quoteVault: string;
  creator: string;
  coinCreator: string;
  isCanonical: boolean;
  /** Raw base-token units held in the base vault. */
  baseReserve: bigint;
  /** Lamports held in the quote (WSOL) vault. */
  quoteReserveLamports: bigint;
  /** Circulating SPL supply of lp_mint. 0 == LP burned/locked (Section 6, H3). */
  lpMintSupply: bigint;
}

export function quoteReserveSol(pool: PoolInfo): number {
  return Number(pool.quoteReserveLamports) / LAMPORTS_PER_SOL;
}

export const BURN_OWNERS: ReadonlySet<string> = new Set([INCINERATOR, DEFAULT_PUBKEY]);

interface DecodedPool {
  poolAddress: string;
  baseMint: string;
  quoteMint: string;
  lpMint: string;
  baseVault: string;
  quoteVault: string;
  creator: string;
  coinCreator: string;
}

/** Decode + validate a Pool account. Returns null if it is not a valid PumpSwap pool. */
export function decodePool(base64Data: string, owner: string, poolAddress: string): DecodedPool | null {
  const buf = Buffer.from(base64Data, 'base64');
  if (owner !== PROGRAM_IDS.PUMP_SWAP) return null;
  if (buf.length < OFF.coin_creator + 32) return null;
  if (buf.subarray(0, 8).toString('hex') !== POOL_DISCRIMINATOR) return null;

  const pk = (o: number): string => base58Encode(buf.subarray(o, o + 32));
  const quoteMint = pk(OFF.quote_mint);
  if (quoteMint !== WSOL_MINT) return null; // only WSOL graduation pools

  return {
    poolAddress,
    baseMint: pk(OFF.base_mint),
    quoteMint,
    lpMint: pk(OFF.lp_mint),
    baseVault: pk(OFF.base_vault),
    quoteVault: pk(OFF.quote_vault),
    creator: pk(OFF.creator),
    coinCreator: pk(OFF.coin_creator),
  };
}

/** SPL token account `amount` (u64 LE) lives at offset 64. */
export function decodeTokenAccountAmount(base64Data: string): bigint {
  const buf = Buffer.from(base64Data, 'base64');
  if (buf.length < 72) return 0n;
  return buf.readBigUInt64LE(64);
}

/**
 * Resolve, decode, and enrich the PumpSwap pool for a mint. Returns null if no
 * valid canonical WSOL pool is found.
 *
 * Fast path: pump.fun's canonical pool address is a deterministic PDA of the
 * mint (verified live against 5 real traded pools — every one matched). One
 * getAccountInfo replaces the getProgramAccounts memcmp scan that used to sit
 * here — getProgramAccounts is one of the heaviest RPC calls (a full
 * account-table scan even with a filter) and was a repeat cause of `pool`
 * missing the enrichment budget. Falls back to the scan only if the derived
 * address doesn't hold a valid pool for this mint — a non-canonical pool, or
 * a mint this bot has never traded before graduating.
 *
 * Reads at 'processed' commitment, not the RpcClient default of 'confirmed'.
 * Detection itself already fires at 'processed' (the earliest possible
 * signal — see heliusWs.ts/laserstream.ts) and screening now starts
 * immediately on that signal (detector/index.ts no longer waits out
 * on-chain confirmation first). 'confirmed' commitment on a pool account
 * created in the same slot we just detected can lag 400-600ms behind
 * 'processed' — reading 'confirmed' here would silently reintroduce most of
 * the latency the earlier fix removed, as a same-sized burst of `pool`
 * unknowns instead of a wait. Consistent with the risk the detector already
 * accepts, not a new one.
 */
export async function fetchPumpSwapPool(rpc: RpcClient, mint: string): Promise<PoolInfo | null> {
  const fast = await fetchCanonicalPool(rpc, mint).catch(() => null);
  const decoded = fast ?? (await fetchPoolBySearch(rpc, mint));
  if (!decoded) return null;
  return finishPool(rpc, decoded);
}

/** Derive + read the canonical pool account directly — no RPC search. */
async function fetchCanonicalPool(rpc: RpcClient, mint: string): Promise<DecodedPool | null> {
  const poolAddress = canonicalPumpPoolPda(new PublicKey(mint)).toBase58();
  const acct = await rpc.getAccountInfoBase64(poolAddress, 'processed');
  if (!acct) return null;
  const decoded = decodePool(acct.data, acct.owner, poolAddress);
  return decoded && decoded.baseMint === mint ? decoded : null;
}

/** Original discovery path: memcmp getProgramAccounts on base_mint. Fallback only. */
async function fetchPoolBySearch(rpc: RpcClient, mint: string): Promise<DecodedPool | null> {
  const accounts = await rpc.getProgramAccountsBase64(
    PROGRAM_IDS.PUMP_SWAP,
    [{ memcmp: { offset: OFF.base_mint, bytes: mint } }],
    'processed',
  );
  for (const a of accounts) {
    const d = decodePool(a.data, a.owner, a.pubkey);
    if (d && d.baseMint === mint) return d;
  }
  return null;
}

async function finishPool(rpc: RpcClient, decoded: DecodedPool): Promise<PoolInfo> {
  const [vaults, lpSupply] = await Promise.all([
    rpc.getMultipleAccountsBase64([decoded.baseVault, decoded.quoteVault], 'processed'),
    rpc.getTokenSupply(decoded.lpMint, 'processed').catch(() => ({ amount: 0n, decimals: 0 })),
  ]);
  const baseAcct = vaults[0];
  const quoteAcct = vaults[1];

  return {
    ...decoded,
    isCanonical: decoded.coinCreator !== DEFAULT_PUBKEY,
    baseReserve: baseAcct ? decodeTokenAccountAmount(baseAcct.data) : 0n,
    quoteReserveLamports: quoteAcct ? decodeTokenAccountAmount(quoteAcct.data) : 0n,
    lpMintSupply: lpSupply.amount,
  };
}
