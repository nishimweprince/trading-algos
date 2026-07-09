import { PublicKey } from '@solana/web3.js';
import { PROGRAM_IDS } from './constants.ts';

const ATA_PROGRAM = new PublicKey(PROGRAM_IDS.ASSOCIATED_TOKEN);

/**
 * Derive the associated token account for (owner, mint), token-program aware.
 * Used to monitor a creator's base-token balance and to read the bot's own
 * post-trade holdings.
 */
export function deriveAta(owner: string, mint: string, isToken2022: boolean): string {
  const tokenProgram = new PublicKey(isToken2022 ? PROGRAM_IDS.TOKEN_2022 : PROGRAM_IDS.TOKEN);
  return PublicKey.findProgramAddressSync(
    [new PublicKey(owner).toBuffer(), tokenProgram.toBuffer(), new PublicKey(mint).toBuffer()],
    ATA_PROGRAM,
  )[0].toBase58();
}
