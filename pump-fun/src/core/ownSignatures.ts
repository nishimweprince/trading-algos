import { appendFileSync, readFileSync } from 'node:fs';

/**
 * Every transaction signature this process signed with the trading wallet.
 *
 * The foreign-signer guard (positions/signerGuard.ts) compares each wallet tx
 * the stream reports against this set: a tx our key paid for that no code
 * here signed means a second writer — another bot instance with the same key
 * (the Kigali box, 2026-10), a leaked key, or an operator acting by hand.
 *
 * Operator CLIs run in their own process, so their signatures never reach
 * the bot's memory. They call `persistOwnSignaturesTo(path)` first; the guard
 * reads that allow-list file before raising.
 */
const MAX_REMEMBERED = 20_000;
const own = new Set<string>();
let persistPath: string | null = null;

export function recordOwnSignature(signature: string): void {
  own.add(signature);
  if (own.size > MAX_REMEMBERED) {
    const oldest = own.values().next().value;
    if (oldest !== undefined) own.delete(oldest);
  }
  if (persistPath) {
    try {
      appendFileSync(persistPath, `${signature}\n`);
    } catch {
      // Best effort: a missing allow-list entry trips the guard (fail safe).
    }
  }
}

export function isOwnSignature(signature: string): boolean {
  return own.has(signature);
}

/** Operator CLIs: also append every signature to the guard's allow-list file. */
export function persistOwnSignaturesTo(path: string): void {
  persistPath = path;
}

/** Signatures operator tools recorded in the allow-list file (empty when absent). */
export function readAllowListedSignatures(path: string): Set<string> {
  try {
    return new Set(readFileSync(path, 'utf8').split('\n').map((l) => l.trim()).filter(Boolean));
  } catch {
    return new Set();
  }
}

/** Test hook. */
export function resetOwnSignatures(): void {
  own.clear();
  persistPath = null;
}
