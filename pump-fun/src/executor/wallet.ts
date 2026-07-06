import { Keypair } from '@solana/web3.js';
import { readSecret } from '../config/load.ts';
import { registerSecret, logger } from '../core/logger.ts';
import { base58Decode } from '../core/base58.ts';
import { WHITELISTED_PROGRAM_IDS } from '../core/constants.ts';
import type { RunMode } from '../config/schema.ts';

/**
 * Wallet management + keypair usage policy (Section 8). The secret is read from
 * the environment only, registered for log redaction, and never persisted. The
 * risk-manager rule that only whitelisted programs may be signed for is enforced
 * here: any transaction touching another program is refused before signing —
 * protection against bugs, not just markets.
 */

export class WalletError extends Error {
  override name = 'WalletError';
}

export class Wallet {
  readonly keypair: Keypair;
  readonly publicKey: string;
  readonly ephemeral: boolean;

  private constructor(keypair: Keypair, ephemeral: boolean) {
    this.keypair = keypair;
    this.publicKey = keypair.publicKey.toBase58();
    this.ephemeral = ephemeral;
  }

  /**
   * Load the wallet for a run mode. `live` requires a real key. `dry-run` may
   * fall back to an ephemeral keypair (real signing + simulation, never sent) so
   * the tx path can be exercised without a funded wallet. `paper` never signs.
   */
  static load(envVar: string, mode: RunMode): Wallet {
    const secret = readSecret(envVar);
    const log = logger.child({ mod: 'wallet' });

    if (!secret) {
      if (mode === 'live') {
        throw new WalletError(`live mode requires ${envVar} to be set`);
      }
      log.warn('no wallet key configured — using an ephemeral keypair (dry-run/sim only)', { mode });
      return new Wallet(Keypair.generate(), true);
    }

    registerSecret(secret);
    return new Wallet(parseSecretKey(secret), false);
  }

  /**
   * Refuse to sign for any program outside the whitelist (Section 8). Callers
   * pass the set of program IDs a transaction touches.
   */
  assertWhitelisted(programIds: readonly string[]): void {
    for (const id of programIds) {
      if (!WHITELISTED_PROGRAM_IDS.includes(id)) {
        throw new WalletError(`refusing to sign — transaction touches non-whitelisted program ${id}`);
      }
    }
  }
}

function parseSecretKey(secret: string): Keypair {
  const s = secret.trim();
  try {
    if (s.startsWith('[')) {
      return Keypair.fromSecretKey(Uint8Array.from(JSON.parse(s) as number[]));
    }
    return Keypair.fromSecretKey(base58Decode(s));
  } catch (err) {
    throw new WalletError(`could not parse wallet secret key: ${(err as Error).message}`);
  }
}
