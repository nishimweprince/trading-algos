import { PublicKey, TransactionInstruction, type AccountMeta } from '@solana/web3.js';
import { PROGRAM_IDS, WSOL_MINT } from '../core/constants.ts';
import type { PoolInfo } from '../enrichment/pool.ts';

/**
 * PumpSwap (`pAMMBay…`) buy/sell instruction builder.
 *
 * Every account and PDA seed below was VERIFIED against real on-chain buy/sell
 * transactions before use (all 8 derivations matched ground truth, and the buy
 * account list is pinned by a golden-fixture test that reproduces a real tx).
 * Discriminators and arg order come from the pump_amm IDL.
 *
 * IMPORTANT — known gap (verified): both the public IDL and the deployed
 * program's on-chain Anchor IDL declare 23 accounts for `buy` (21 for `sell`),
 * and this builder matches that exactly. BUT sampled live transactions
 * consistently carry 2+ additional `remaining_accounts` after `fee_program`
 * that are NOT in either IDL and vary per transaction. Their derivation is
 * undocumented; resolving it (via the pump-amm SDK source or dry-run
 * simulation) is required before these swaps will succeed on-chain. The
 * broadcaster's simulate-then-refuse gate guarantees a wrong/incomplete account
 * set can never reach a live send — it just fails simulation.
 */

const PUMP_SWAP = new PublicKey(PROGRAM_IDS.PUMP_SWAP);
const TOKEN = new PublicKey(PROGRAM_IDS.TOKEN);
const TOKEN_2022 = new PublicKey(PROGRAM_IDS.TOKEN_2022);
const SYSTEM = new PublicKey(PROGRAM_IDS.SYSTEM);
const WSOL = new PublicKey(WSOL_MINT);

// Verified fixed addresses (identical across sampled txs / official constants).
export const FEE_PROGRAM = new PublicKey('pfeeUxB6jkeY1Hxd7CsFCAjcbHA9rWtchMGdZ6VojVZ');
export const GLOBAL_CONFIG = new PublicKey('ADyA8hdefvWN2dbGGWFotbzWxrAvLW83WG6QCVXvJKqw');
export const PROTOCOL_FEE_RECIPIENT = new PublicKey('62qc2CNXwrYqQScmEdiZFFAnJR262PxWEuNQtxfafNgV');
export const ATA_PROGRAM = new PublicKey('ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL');

const BUY_DISCRIMINATOR = Buffer.from([102, 6, 61, 18, 1, 218, 235, 234]);
const SELL_DISCRIMINATOR = Buffer.from([51, 230, 133, 164, 1, 127, 131, 173]);

function pda(seeds: Buffer[], program: PublicKey = PUMP_SWAP): PublicKey {
  return PublicKey.findProgramAddressSync(seeds, program)[0];
}

/** Associated token address (mint's token program aware). */
export function ataFor(owner: PublicKey, mint: PublicKey, tokenProgram: PublicKey): PublicKey {
  return PublicKey.findProgramAddressSync(
    [owner.toBuffer(), tokenProgram.toBuffer(), mint.toBuffer()],
    ATA_PROGRAM,
  )[0];
}

export const eventAuthority = (): PublicKey => pda([Buffer.from('__event_authority')]);
export const globalVolumeAccumulator = (): PublicKey => pda([Buffer.from('global_volume_accumulator')]);
export const feeConfig = (): PublicKey => pda([Buffer.from('fee_config'), PUMP_SWAP.toBuffer()], FEE_PROGRAM);
export const coinCreatorVaultAuthority = (coinCreator: PublicKey): PublicKey =>
  pda([Buffer.from('creator_vault'), coinCreator.toBuffer()]);
export const userVolumeAccumulator = (user: PublicKey): PublicKey =>
  pda([Buffer.from('user_volume_accumulator'), user.toBuffer()]);

function u64le(v: bigint): Buffer {
  const b = Buffer.alloc(8);
  b.writeBigUInt64LE(v);
  return b;
}

const meta = (pubkey: PublicKey, isWritable: boolean, isSigner = false): AccountMeta => ({ pubkey, isWritable, isSigner });

export interface BuyParams {
  pool: PoolInfo;
  user: PublicKey;
  baseIsToken2022: boolean;
  /** Base tokens to receive (raw units). */
  baseAmountOut: bigint;
  /** Max quote (WSOL lamports) to spend — slippage bound. */
  maxQuoteIn: bigint;
  /** track_volume flag (updates the volume accumulators). */
  trackVolume?: boolean;
}

export function buildBuyInstruction(p: BuyParams): TransactionInstruction {
  const baseProg = p.baseIsToken2022 ? TOKEN_2022 : TOKEN;
  const pool = p.pool;
  const poolPk = new PublicKey(pool.poolAddress);
  const baseMint = new PublicKey(pool.baseMint);
  const vaultAuth = coinCreatorVaultAuthority(new PublicKey(pool.coinCreator));

  const keys: AccountMeta[] = [
    meta(poolPk, true),
    meta(p.user, true, true),
    meta(GLOBAL_CONFIG, false),
    meta(baseMint, false),
    meta(WSOL, false),
    meta(ataFor(p.user, baseMint, baseProg), true),
    meta(ataFor(p.user, WSOL, TOKEN), true),
    meta(new PublicKey(pool.baseVault), true),
    meta(new PublicKey(pool.quoteVault), true),
    meta(PROTOCOL_FEE_RECIPIENT, false),
    meta(ataFor(PROTOCOL_FEE_RECIPIENT, WSOL, TOKEN), true),
    meta(baseProg, false),
    meta(TOKEN, false),
    meta(SYSTEM, false),
    meta(ATA_PROGRAM, false),
    meta(eventAuthority(), false),
    meta(PUMP_SWAP, false),
    meta(ataFor(vaultAuth, WSOL, TOKEN), true),
    meta(vaultAuth, false),
    meta(globalVolumeAccumulator(), false),
    meta(userVolumeAccumulator(p.user), true),
    meta(feeConfig(), false),
    meta(FEE_PROGRAM, false),
  ];

  const data = Buffer.concat([
    BUY_DISCRIMINATOR,
    u64le(p.baseAmountOut),
    u64le(p.maxQuoteIn),
    Buffer.from([p.trackVolume === false ? 0 : 1]), // track_volume: OptionBool
  ]);

  return new TransactionInstruction({ programId: PUMP_SWAP, keys, data });
}

export interface SellParams {
  pool: PoolInfo;
  user: PublicKey;
  baseIsToken2022: boolean;
  /** Base tokens to sell (raw units). */
  baseAmountIn: bigint;
  /** Min quote (WSOL lamports) to receive — slippage bound. */
  minQuoteOut: bigint;
}

export function buildSellInstruction(p: SellParams): TransactionInstruction {
  const baseProg = p.baseIsToken2022 ? TOKEN_2022 : TOKEN;
  const pool = p.pool;
  const poolPk = new PublicKey(pool.poolAddress);
  const baseMint = new PublicKey(pool.baseMint);
  const vaultAuth = coinCreatorVaultAuthority(new PublicKey(pool.coinCreator));

  // Sell has the same layout as buy minus the volume accumulators.
  const keys: AccountMeta[] = [
    meta(poolPk, true),
    meta(p.user, true, true),
    meta(GLOBAL_CONFIG, false),
    meta(baseMint, false),
    meta(WSOL, false),
    meta(ataFor(p.user, baseMint, baseProg), true),
    meta(ataFor(p.user, WSOL, TOKEN), true),
    meta(new PublicKey(pool.baseVault), true),
    meta(new PublicKey(pool.quoteVault), true),
    meta(PROTOCOL_FEE_RECIPIENT, false),
    meta(ataFor(PROTOCOL_FEE_RECIPIENT, WSOL, TOKEN), true),
    meta(baseProg, false),
    meta(TOKEN, false),
    meta(SYSTEM, false),
    meta(ATA_PROGRAM, false),
    meta(eventAuthority(), false),
    meta(PUMP_SWAP, false),
    meta(ataFor(vaultAuth, WSOL, TOKEN), true),
    meta(vaultAuth, false),
    meta(feeConfig(), false),
    meta(FEE_PROGRAM, false),
  ];

  const data = Buffer.concat([SELL_DISCRIMINATOR, u64le(p.baseAmountIn), u64le(p.minQuoteOut)]);
  return new TransactionInstruction({ programId: PUMP_SWAP, keys, data });
}
