import { registerSecret } from './logger.ts';

/**
 * Minimal Solana JSON-RPC client over Node's global fetch. Kept dependency-free
 * for Phase 1 (confirmation + a couple of reads); richer account decoding
 * arrives with the enrichment layer in Phase 2.
 *
 * The endpoint URL embeds the Helius API key, so both the full URL and the
 * extracted api-key are registered as secrets for log redaction.
 */

export interface RpcClientOptions {
  httpUrl: string;
  timeoutMs?: number;
}

export class RpcError extends Error {
  override name = 'RpcError';
}

interface JsonRpcResponse<T> {
  result?: T;
  error?: { code: number; message: string };
}

export interface TransactionConfirmation {
  slot: number;
  blockTime: number | null;
  err: unknown;
}

export class RpcClient {
  private readonly url: string;
  private readonly timeoutMs: number;
  private id = 0;

  constructor(opts: RpcClientOptions) {
    this.url = opts.httpUrl;
    this.timeoutMs = opts.timeoutMs ?? 5_000;
    registerSecret(this.url);
    const key = new URL(this.url).searchParams.get('api-key');
    if (key) registerSecret(key);
  }

  private async call<T>(method: string, params: unknown[]): Promise<T> {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), this.timeoutMs);
    try {
      const res = await fetch(this.url, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ jsonrpc: '2.0', id: ++this.id, method, params }),
        signal: controller.signal,
      });
      if (!res.ok) throw new RpcError(`${method} HTTP ${res.status}`);
      const body = (await res.json()) as JsonRpcResponse<T>;
      if (body.error) throw new RpcError(`${method}: ${body.error.message} (${body.error.code})`);
      return body.result as T;
    } catch (err) {
      if (err instanceof RpcError) throw err;
      if ((err as Error).name === 'AbortError') {
        throw new RpcError(`${method} timed out after ${this.timeoutMs}ms`);
      }
      throw new RpcError(`${method} failed: ${(err as Error).message}`);
    } finally {
      clearTimeout(timer);
    }
  }

  async getSlot(commitment: 'processed' | 'confirmed' | 'finalized' = 'confirmed'): Promise<number> {
    return this.call<number>('getSlot', [{ commitment }]);
  }

  /** Health check used at startup. Returns true when the node reports "ok". */
  async getHealth(): Promise<boolean> {
    try {
      const r = await this.call<string>('getHealth', []);
      return r === 'ok';
    } catch {
      return false;
    }
  }

  /**
   * Fetch a transaction and return its confirmation status. Returns null when
   * the signature is not yet visible (not confirmed / not found).
   */
  async getTransactionConfirmation(
    signature: string,
    commitment: 'confirmed' | 'finalized' = 'confirmed',
  ): Promise<TransactionConfirmation | null> {
    const result = await this.call<{
      slot: number;
      blockTime: number | null;
      meta: { err: unknown } | null;
    } | null>('getTransaction', [
      signature,
      { commitment, maxSupportedTransactionVersion: 0 },
    ]);
    if (!result) return null;
    return { slot: result.slot, blockTime: result.blockTime, err: result.meta?.err ?? null };
  }

  /** Raw base64 account data, or null if the account does not exist. */
  async getAccountInfoBase64(
    pubkey: string,
    commitment: 'processed' | 'confirmed' | 'finalized' = 'confirmed',
  ): Promise<{ data: string; owner: string; lamports: number } | null> {
    const result = await this.call<{
      value: { data: [string, string]; owner: string; lamports: number } | null;
    }>('getAccountInfo', [pubkey, { encoding: 'base64', commitment }]);
    const v = result.value;
    if (!v) return null;
    return { data: v.data[0], owner: v.owner, lamports: v.lamports };
  }
}
