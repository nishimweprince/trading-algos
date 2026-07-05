/**
 * Minimal base58 (Bitcoin alphabet) codec. Hand-rolled to keep the dependency
 * surface small for a wallet-signing trading bot. Used to read 32-byte pubkeys
 * out of raw account data and compare them against well-known addresses.
 */

const ALPHABET = '123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz';
const BASE = 58n;

export function base58Encode(bytes: Uint8Array): string {
  if (bytes.length === 0) return '';
  let zeros = 0;
  while (zeros < bytes.length && bytes[zeros] === 0) zeros++;

  let num = 0n;
  for (const b of bytes) num = num * 256n + BigInt(b);

  let out = '';
  while (num > 0n) {
    const rem = Number(num % BASE);
    num /= BASE;
    out = ALPHABET[rem] + out;
  }
  return '1'.repeat(zeros) + out;
}

export function base58Decode(str: string): Uint8Array {
  if (str.length === 0) return new Uint8Array();
  let num = 0n;
  for (const ch of str) {
    const idx = ALPHABET.indexOf(ch);
    if (idx < 0) throw new Error(`invalid base58 character: ${ch}`);
    num = num * BASE + BigInt(idx);
  }

  const bytes: number[] = [];
  while (num > 0n) {
    bytes.unshift(Number(num % 256n));
    num /= 256n;
  }

  let zeros = 0;
  while (zeros < str.length && str[zeros] === '1') zeros++;
  for (let i = 0; i < zeros; i++) bytes.unshift(0);

  return new Uint8Array(bytes);
}

/** The system-program default pubkey (all zeros) — used as the "revoked" sentinel. */
export const DEFAULT_PUBKEY = '11111111111111111111111111111111';
