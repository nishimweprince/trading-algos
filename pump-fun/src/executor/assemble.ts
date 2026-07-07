import {
  ComputeBudgetProgram,
  Connection,
  PublicKey,
  SystemProgram,
  TransactionMessage,
  VersionedTransaction,
  type TransactionInstruction,
} from '@solana/web3.js';
import type { Wallet } from './wallet.ts';
import type { FeePlan } from './fees.ts';

/**
 * Assemble a signed v0 transaction from swap instructions (Section 7.1):
 * compute-unit limit + priority fee, the swap, and an optional Jito tip transfer.
 * The wallet's whitelist policy is enforced on every instruction before signing.
 */

const DEFAULT_CU_LIMIT = 250_000;

export interface AssembleDeps {
  connection: Connection;
  wallet: Wallet;
  feePlan: FeePlan;
  computeUnitLimit?: number;
  /** Jito tip recipient; tip is only added when set and feePlan.jitoTipLamports > 0. */
  jitoTipAccount?: string;
}

export async function assembleSignedSwapTx(
  swapIxs: TransactionInstruction[],
  deps: AssembleDeps,
): Promise<Uint8Array> {
  const payer = deps.wallet.keypair.publicKey;

  const ixs: TransactionInstruction[] = [
    ComputeBudgetProgram.setComputeUnitLimit({ units: deps.computeUnitLimit ?? DEFAULT_CU_LIMIT }),
    ComputeBudgetProgram.setComputeUnitPrice({ microLamports: deps.feePlan.priorityMicroLamports }),
    ...swapIxs,
  ];

  if (deps.feePlan.jitoTipLamports > 0 && deps.jitoTipAccount) {
    ixs.push(
      SystemProgram.transfer({
        fromPubkey: payer,
        toPubkey: new PublicKey(deps.jitoTipAccount),
        lamports: deps.feePlan.jitoTipLamports,
      }),
    );
  }

  // Enforce the keypair-usage policy before signing (Section 8).
  deps.wallet.assertWhitelisted([...new Set(ixs.map((i) => i.programId.toBase58()))]);

  const { blockhash } = await deps.connection.getLatestBlockhash('confirmed');
  const message = new TransactionMessage({
    payerKey: payer,
    recentBlockhash: blockhash,
    instructions: ixs,
  }).compileToV0Message();

  const tx = new VersionedTransaction(message);
  tx.sign([deps.wallet.keypair]);
  return tx.serialize();
}
