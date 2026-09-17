import { Connection, VersionedTransaction } from '@solana/web3.js';
import type { TxSender, TxSendResult } from './broadcaster.ts';
import { withTimeout } from './timeout.ts';

/**
 * Concrete broadcaster send path over a web3.js Connection. `simulate` runs
 * `simulateTransaction` (used by dry-run and the pre-send check); `send` uses
 * `sendRawTransaction` with preflight skipped (we simulate separately, and on
 * the exit hot path every millisecond counts).
 *
 * Multiple instances (primary RPC, secondary RPC) are passed to the Broadcaster
 * for multi-path live sends. A Jito bundle path is added with the paid infra.
 */
export class RpcTxSender implements TxSender {
  readonly name: string;
  private readonly connection: Connection;

  private readonly commitment: 'processed' | 'confirmed';
  private readonly simulateTimeoutMs: number;

  constructor(
    name: string,
    httpUrl: string,
    opts: { commitment?: 'processed' | 'confirmed'; simulateTimeoutMs?: number } = {},
  ) {
    this.name = name;
    this.commitment = opts.commitment ?? 'confirmed';
    this.simulateTimeoutMs = opts.simulateTimeoutMs ?? 12_000;
    this.connection = new Connection(httpUrl, this.commitment);
  }

  async simulate(txBytes: Uint8Array): Promise<{ err: unknown; logs: string[] }> {
    const tx = VersionedTransaction.deserialize(txBytes);
    const res = await withTimeout(
      this.connection.simulateTransaction(tx, {
        sigVerify: false,
        replaceRecentBlockhash: true,
        commitment: this.commitment,
      }),
      this.simulateTimeoutMs,
      `${this.name} simulate`,
    );
    return { err: res.value.err, logs: res.value.logs ?? [] };
  }

  async send(txBytes: Uint8Array): Promise<TxSendResult> {
    const signature = await this.connection.sendRawTransaction(txBytes, {
      skipPreflight: true,
      maxRetries: 0,
    });
    return { signature };
  }
}
