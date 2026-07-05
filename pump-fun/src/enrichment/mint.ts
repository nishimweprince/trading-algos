import { base58Encode, DEFAULT_PUBKEY } from '../core/base58.ts';
import { PROGRAM_IDS } from '../core/constants.ts';

/**
 * Decode an SPL Token / Token-2022 mint account from raw bytes. Layouts are
 * fixed and stable, so we decode directly rather than pulling in web3.js — this
 * keeps the safety-critical path dependency-light and auditable.
 *
 * SPL Mint (82 bytes):
 *   0..4   mintAuthority COption tag (u32 LE; 1 = present)
 *   4..36  mintAuthority pubkey
 *   36..44 supply (u64 LE)
 *   44     decimals (u8)
 *   45     isInitialized (u8)
 *   46..50 freezeAuthority COption tag (u32 LE)
 *   50..82 freezeAuthority pubkey
 *
 * Token-2022 mints share those 82 bytes; when extensions are present the account
 * is padded to >=166 bytes with an accountType byte at offset 165 and a TLV
 * extension list from offset 166.
 */

/** Token-2022 mint-level extension types that are programmable rug vectors (Section 6, H9). */
export const MintExtension = {
  TransferFeeConfig: 1,
  MintCloseAuthority: 3,
  DefaultAccountState: 6,
  NonTransferable: 9,
  InterestBearingConfig: 10,
  PermanentDelegate: 12,
  TransferHook: 14,
} as const;

export type MintExtensionName = keyof typeof MintExtension;

/**
 * Extensions that are programmable rug vectors (Section 6, H9). Benign
 * extensions pump.fun issues (metadata pointer, token metadata, group pointer)
 * are deliberately absent — a Token-2022 mint carrying only those is safe.
 */
export const RUG_EXTENSION_IDS: ReadonlySet<number> = new Set<number>([
  MintExtension.TransferFeeConfig,
  MintExtension.TransferHook,
  MintExtension.PermanentDelegate,
  MintExtension.DefaultAccountState,
  MintExtension.NonTransferable,
]);

export function hasRugExtensions(extensions: number[]): boolean {
  return extensions.some((e) => RUG_EXTENSION_IDS.has(e));
}

export const RUG_EXTENSION_NAMES: Record<number, string> = {
  [MintExtension.TransferFeeConfig]: 'TransferFee',
  [MintExtension.TransferHook]: 'TransferHook',
  [MintExtension.PermanentDelegate]: 'PermanentDelegate',
  [MintExtension.DefaultAccountState]: 'DefaultAccountState',
  [MintExtension.NonTransferable]: 'NonTransferable',
};

export interface MintInfo {
  isToken2022: boolean;
  mintAuthority: string | null; // null == revoked
  freezeAuthority: string | null; // null == revoked
  supply: bigint;
  decimals: number;
  /** Token-2022 extension type ids present on the mint. */
  extensions: number[];
}

const MINT_LEN = 82;
const ACCOUNT_TYPE_OFFSET = 165;
const TLV_START = 166;

function readPubkey(buf: Buffer, offset: number): string {
  return base58Encode(buf.subarray(offset, offset + 32));
}

function readCOptionPubkey(buf: Buffer, tagOffset: number): string | null {
  const tag = buf.readUInt32LE(tagOffset);
  if (tag === 0) return null;
  const pk = readPubkey(buf, tagOffset + 4);
  // Defensive: an all-zero "present" authority is effectively revoked.
  return pk === DEFAULT_PUBKEY ? null : pk;
}

export function decodeMint(base64Data: string, owner: string): MintInfo {
  const buf = Buffer.from(base64Data, 'base64');
  if (buf.length < MINT_LEN) {
    throw new Error(`mint account too short: ${buf.length} bytes`);
  }

  const isToken2022 = owner === PROGRAM_IDS.TOKEN_2022;
  const mintAuthority = readCOptionPubkey(buf, 0);
  const supply = buf.readBigUInt64LE(36);
  const decimals = buf.readUInt8(44);
  const freezeAuthority = readCOptionPubkey(buf, 46);

  const extensions =
    isToken2022 && buf.length > ACCOUNT_TYPE_OFFSET ? parseExtensions(buf) : [];

  return { isToken2022, mintAuthority, freezeAuthority, supply, decimals, extensions };
}

function parseExtensions(buf: Buffer): number[] {
  const found: number[] = [];
  let cursor = TLV_START;
  while (cursor + 4 <= buf.length) {
    const type = buf.readUInt16LE(cursor);
    const len = buf.readUInt16LE(cursor + 2);
    if (type === 0) break; // Uninitialized terminator
    found.push(type);
    cursor += 4 + len;
  }
  return found;
}
