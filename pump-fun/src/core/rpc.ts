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
  /** Transient-failure retries per call (429/5xx/network/timeout). Default 2. */
  retries?: number;
  /**
   * Max concurrent in-flight requests. The enrichment burst fires ~8 calls at
   * once; capping concurrency smooths it under the free-tier rate limit so
   * checkable fields (holders → H5/H6) resolve instead of degrading to unknown.
   * Default 4.
   */
  maxConcurrent?: number;
}

/** Minimal FIFO semaphore to bound concurrent RPC requests. */
class Semaphore {
  private active = 0;
  private readonly queue: Array<() => void> = [];
  private readonly max: number;
  constructor(max: number) {
    this.max = max;
  }

  async acquire(): Promise<() => void> {
    if (this.active < this.max) {
      this.active++;
      return () => this.release();
    }
    return new Promise((resolve) => {
      this.queue.push(() => {
        this.active++;
        resolve(() => this.release());
      });
    });
  }

  private release(): void {
    this.active--;
    this.queue.shift()?.();
  }
}

export class RpcError extends Error {
  override name = 'RpcError';
  /** True for transient failures worth retrying (rate limit, 5xx, network). */
  retryable = false;
  constructor(message: string, retryable = false) {
    super(message);
    this.retryable = retryable;
  }
}

const RETRY_BASE_MS = 120;

interface JsonRpcResponse<T> {
  result?: T;
  error?: { code: number; message: string };
}

function delay(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

export interface TransactionConfirmation {
  slot: number;
  blockTime: number | null;
  err: unknown;
}

export class RpcClient {
  private readonly url: string;
  private readonly timeoutMs: number;
  private readonly retries: number;
  private readonly semaphore: Semaphore;
  private id = 0;

  constructor(opts: RpcClientOptions) {
    this.url = opts.httpUrl;
    this.timeoutMs = opts.timeoutMs ?? 5_000;
    this.retries = opts.retries ?? 2;
    this.semaphore = new Semaphore(opts.maxConcurrent ?? 4);
    registerSecret(this.url);
    const key = new URL(this.url).searchParams.get('api-key');
    if (key) registerSecret(key);
  }

  /**
   * One JSON-RPC round trip. Retries transient failures (rate limit, 5xx,
   * network, timeout) with short backoff — the concurrent enrichment burst can
   * momentarily trip the free-tier rate limit, and a retry keeps a checkable
   * field from silently degrading to `unknown`.
   */
  private async call<T>(method: string, params: unknown[]): Promise<T> {
    let lastErr: RpcError | undefined;
    for (let attempt = 0; attempt <= this.retries; attempt++) {
      try {
        return await this.attempt<T>(method, params);
      } catch (err) {
        lastErr = err as RpcError;
        if (!lastErr.retryable || attempt === this.retries) throw lastErr;
        await delay(RETRY_BASE_MS * 2 ** attempt);
      }
    }
    throw lastErr ?? new RpcError(`${method} failed`);
  }

  private async attempt<T>(method: string, params: unknown[]): Promise<T> {
    const release = await this.semaphore.acquire();
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), this.timeoutMs);
    try {
      const res = await fetch(this.url, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ jsonrpc: '2.0', id: ++this.id, method, params }),
        signal: controller.signal,
      });
      if (!res.ok) throw new RpcError(`${method} HTTP ${res.status}`, res.status === 429 || res.status >= 500);
      const body = (await res.json()) as JsonRpcResponse<T>;
      if (body.error) {
        const rateLimited = body.error.code === -32005 || /rate|limit|busy/i.test(body.error.message);
        throw new RpcError(`${method}: ${body.error.message} (${body.error.code})`, rateLimited);
      }
      return body.result as T;
    } catch (err) {
      if (err instanceof RpcError) throw err;
      if ((err as Error).name === 'AbortError') {
        throw new RpcError(`${method} timed out after ${this.timeoutMs}ms`, true);
      }
      throw new RpcError(`${method} failed: ${(err as Error).message}`, true);
    } finally {
      clearTimeout(timer);
      release();
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

  /** Batch account fetch (base64). Positionally aligned with `pubkeys`. */
  async getMultipleAccountsBase64(
    pubkeys: string[],
    commitment: 'processed' | 'confirmed' | 'finalized' = 'confirmed',
  ): Promise<Array<{ data: string; owner: string; lamports: number } | null>> {
    if (pubkeys.length === 0) return [];
    const result = await this.call<{
      value: Array<{ data: [string, string]; owner: string; lamports: number } | null>;
    }>('getMultipleAccounts', [pubkeys, { encoding: 'base64', commitment }]);
    return result.value.map((v) => (v ? { data: v.data[0], owner: v.owner, lamports: v.lamports } : null));
  }

  /** Total supply of a mint (raw base units as bigint + decimals). */
  async getTokenSupply(mint: string): Promise<{ amount: bigint; decimals: number }> {
    const result = await this.call<{ value: { amount: string; decimals: number } }>(
      'getTokenSupply',
      [mint, { commitment: 'confirmed' }],
    );
    return { amount: BigInt(result.value.amount), decimals: result.value.decimals };
  }

  /** Up to the 20 largest token accounts for a mint (token-account addresses). */
  async getTokenLargestAccounts(
    mint: string,
  ): Promise<Array<{ address: string; amount: bigint }>> {
    const result = await this.call<{ value: Array<{ address: string; amount: string }> }>(
      'getTokenLargestAccounts',
      [mint, { commitment: 'confirmed' }],
    );
    return result.value.map((a) => ({ address: a.address, amount: BigInt(a.amount) }));
  }

  /**
   * getProgramAccounts (base64) with filters. Used to resolve a PumpSwap pool by
   * a memcmp on base_mint. Filtered to a single mint, so the result set is tiny.
   */
  async getProgramAccountsBase64(
    programId: string,
    filters: Array<{ memcmp: { offset: number; bytes: string } } | { dataSize: number }>,
    commitment: 'processed' | 'confirmed' | 'finalized' = 'confirmed',
  ): Promise<Array<{ pubkey: string; data: string; owner: string }>> {
    const result = await this.call<
      Array<{ pubkey: string; account: { data: [string, string]; owner: string } }>
    >('getProgramAccounts', [programId, { encoding: 'base64', commitment, filters }]);
    return result.map((r) => ({ pubkey: r.pubkey, data: r.account.data[0], owner: r.account.owner }));
  }

  /** Recent prioritization fees (micro-lamports/CU) for percentile fee sizing. */
  async getRecentPrioritizationFees(addresses: string[] = []): Promise<number[]> {
    const result = await this.call<Array<{ slot: number; prioritizationFee: number }>>(
      'getRecentPrioritizationFees',
      addresses.length ? [addresses] : [],
    );
    return result.map((r) => r.prioritizationFee);
  }

  /** Helius DAS getAsset — metadata + authorities. Advisory (soft signals). */
  async getAsset(mint: string): Promise<DasAsset | null> {
    try {
      return await this.call<DasAsset>('getAsset', [{ id: mint }]);
    } catch {
      return null;
    }
  }
}

/** Partial shape of a Helius DAS asset — only the fields we consume. */
export interface DasAsset {
  content?: {
    metadata?: { name?: string; symbol?: string };
    links?: Record<string, string>;
    json_uri?: string;
  };
  authorities?: Array<{ address: string; scopes: string[] }>;
  token_info?: { supply?: number; decimals?: number };
}
