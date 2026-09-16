import {
  ComputeBudgetProgram,
  Connection,
  PublicKey,
  TransactionInstruction,
  TransactionMessage,
  VersionedTransaction,
} from '@solana/web3.js';
import { PROGRAM_IDS } from '../core/constants.ts';
import { logger } from '../core/logger.ts';
import type { Wallet } from './wallet.ts';

/**
 * Empty-ATA sweeper.
 *
 * Every live buy creates an associated token account for the mint (rent
 * ~0.00204 SOL) and nothing closes it after the sell — the pilot wallet already
 * holds 9 empty ATAs from earlier trades. At ~100 live trades a day that is
 * ~0.2 SOL/day locked in rent, which would walk a 1 SOL wallet into the
 * WALLET_FLOOR breaker in days. Rent is refundable: closing an empty account
 * returns its lamports to the owner.
 *
 * Deliberately NOT attached to the sell transaction: `CloseAccount` fails on a
 * non-zero balance, so a partial fill would take the whole exit down with it.
 * Instead this runs off the exit hot path — at boot and on a timer — and only
 * touches accounts that are already at zero.
 */

export interface EmptyAta {
  address: string;
  mint: string;
  programId: string;
  lamports: number;
}

export interface SweepResult {
  found: number;
  closed: number;
  lamportsReclaimed: number;
  signatures: string[];
  errors: string[];
}

/** SPL Token / Token-2022 `CloseAccount` = instruction index 9, no data args. */
const CLOSE_ACCOUNT_IX = 9;
/** Enough for ~20 CloseAccount ixs; keeps the tx well under the size limit. */
const MAX_CLOSES_PER_TX = 20;
const CU_LIMIT = 60_000;

export async function listEmptyTokenAccounts(connection: Connection, owner: PublicKey): Promise<EmptyAta[]> {
  const out: EmptyAta[] = [];
  for (const programId of [PROGRAM_IDS.TOKEN, PROGRAM_IDS.TOKEN_2022]) {
    const res = await connection.getParsedTokenAccountsByOwner(owner, { programId: new PublicKey(programId) });
    for (const { pubkey, account } of res.value) {
      const info = (account.data as { parsed?: { info?: { mint?: string; tokenAmount?: { amount?: string } } } })
        .parsed?.info;
      if (!info?.mint || info.tokenAmount?.amount !== '0') continue;
      out.push({ address: pubkey.toBase58(), mint: info.mint, programId, lamports: account.lamports });
    }
  }
  return out;
}

export function closeAccountInstruction(account: string, owner: PublicKey, programId: string): TransactionInstruction {
  return new TransactionInstruction({
    programId: new PublicKey(programId),
    keys: [
      { pubkey: new PublicKey(account), isSigner: false, isWritable: true },
      { pubkey: owner, isSigner: false, isWritable: true }, // rent destination
      { pubkey: owner, isSigner: true, isWritable: false }, // authority
    ],
    data: Buffer.from([CLOSE_ACCOUNT_IX]),
  });
}

/**
 * Close every empty token account the wallet owns, batching up to
 * MAX_CLOSES_PER_TX per transaction. `dryRun` lists without sending.
 */
export async function sweepEmptyTokenAccounts(
  connection: Connection,
  wallet: Wallet,
  opts: { dryRun?: boolean; priorityMicroLamports?: number } = {},
): Promise<SweepResult> {
  const log = logger.child({ mod: 'ata-sweeper' });
  const owner = wallet.keypair.publicKey;
  const empties = await listEmptyTokenAccounts(connection, owner);
  const result: SweepResult = {
    found: empties.length,
    closed: 0,
    lamportsReclaimed: 0,
    signatures: [],
    errors: [],
  };
  if (empties.length === 0 || opts.dryRun) return result;

  for (let i = 0; i < empties.length; i += MAX_CLOSES_PER_TX) {
    const batch = empties.slice(i, i + MAX_CLOSES_PER_TX);
    const ixs = [
      ComputeBudgetProgram.setComputeUnitLimit({ units: CU_LIMIT }),
      ComputeBudgetProgram.setComputeUnitPrice({ microLamports: opts.priorityMicroLamports ?? 50_000 }),
      ...batch.map((a) => closeAccountInstruction(a.address, owner, a.programId)),
    ];
    wallet.assertWhitelisted([...new Set(ixs.map((ix) => ix.programId.toBase58()))]);
    try {
      const { blockhash, lastValidBlockHeight } = await connection.getLatestBlockhash('confirmed');
      const tx = new VersionedTransaction(
        new TransactionMessage({ payerKey: owner, recentBlockhash: blockhash, instructions: ixs }).compileToV0Message(),
      );
      tx.sign([wallet.keypair]);
      const signature = await connection.sendTransaction(tx, { skipPreflight: false, maxRetries: 3 });
      const conf = await connection.confirmTransaction({ signature, blockhash, lastValidBlockHeight }, 'confirmed');
      if (conf.value.err) {
        result.errors.push(`${signature}: ${JSON.stringify(conf.value.err)}`);
        continue;
      }
      result.signatures.push(signature);
      result.closed += batch.length;
      result.lamportsReclaimed += batch.reduce((s, a) => s + a.lamports, 0);
      log.info('closed empty token accounts', { count: batch.length, signature });
    } catch (err) {
      result.errors.push(err instanceof Error ? err.message : String(err));
      log.warn('ATA sweep batch failed — will retry next sweep', { err });
    }
  }
  return result;
}
