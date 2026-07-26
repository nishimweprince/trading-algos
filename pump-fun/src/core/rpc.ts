import { logger, registerSecret } from './logger.ts';

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
  /**
   * Additional independent read endpoints, tried in order when the primary is
   * rate-limited or down. Reads are idempotent, so failing over is safe.
   *
   * This matters more than it looks: when the only endpoint is exhausted, every
   * enrichment field degrades to `unknown`, and in live mode unknowns are
   * vetoes — so a dead RPC silently becomes a 100% veto rate rather than a
   * visible outage.
   */
  fallbackHttpUrls?: string[];
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
/**
 * How long a rate-limited endpoint is skipped for. Long enough that a burst of
 * calls doesn't keep re-probing an exhausted plan, short enough that a
 * recovered endpoint is picked back up quickly.
 */
const ENDPOINT_COOLDOWN_MS = 30_000;

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

interface Endpoint {
  url: string;
  /** Epoch ms until which this endpoint is skipped (0 = healthy). */
  cooldownUntil: number;
}

export class RpcClient {
  private readonly endpoints: Endpoint[];
  private readonly timeoutMs: number;
  private readonly retries: number;
  private readonly semaphore: Semaphore;
  private readonly log = logger.child({ mod: 'rpc' });
  private readonly now: () => number;
  private id = 0;
  private lastServedBy: string | null = null;

  constructor(opts: RpcClientOptions & { now?: () => number }) {
    const urls = [opts.httpUrl, ...(opts.fallbackHttpUrls ?? [])].filter(
      (u, i, all) => u && all.indexOf(u) === i,
    );
    this.endpoints = urls.map((url) => ({ url, cooldownUntil: 0 }));
    this.timeoutMs = opts.timeoutMs ?? 5_000;
    this.retries = opts.retries ?? 2;
    this.semaphore = new Semaphore(opts.maxConcurrent ?? 4);
    this.now = opts.now ?? (() => Date.now());
    for (const { url } of this.endpoints) {
      registerSecret(url);
      const key = new URL(url).searchParams.get('api-key');
      if (key) registerSecret(key);
    }
  }

  /** Primary URL — kept for callers that need an http endpoint string. */
  get url(): string {
    return this.endpoints[0]!.url;
  }

  /** True when every configured endpoint is currently cooling down. */
  get allEndpointsCoolingDown(): boolean {
    const now = this.now();
    return this.endpoints.every((e) => e.cooldownUntil > now);
  }

  /**
   * Next endpoint to try. Prefers the first healthy one (so the primary is
   * always favoured once it recovers); if all are cooling down, uses the one
   * that recovers soonest rather than giving up.
   */
  private pickEndpoint(skip: Set<string>): Endpoint {
    const now = this.now();
    const usable = this.endpoints.filter((e) => !skip.has(e.url));
    const pool = usable.length > 0 ? usable : this.endpoints;
    return (
      pool.find((e) => e.cooldownUntil <= now) ??
      pool.reduce((soonest, e) => (e.cooldownUntil < soonest.cooldownUntil ? e : soonest))
    );
  }

  /**
   * One JSON-RPC round trip. Retries transient failures (rate limit, 5xx,
   * network, timeout) with short backoff — the concurrent enrichment burst can
   * momentarily trip the free-tier rate limit, and a retry keeps a checkable
   * field from silently degrading to `unknown`.
   */
  private async call<T>(method: string, params: unknown[]): Promise<T> {
    let lastErr: RpcError | undefined;
    // Endpoints already tried for THIS call, so one call never burns two
    // attempts on the same failing host while a healthy one sits unused.
    const tried = new Set<string>();
    // Total attempts. With one endpoint this is exactly `retries + 1` — the
    // pre-failover behaviour, unchanged. Each extra endpoint buys one more
    // attempt, so failover never eats the single-host retry budget.
    const budget = this.retries + this.endpoints.length;

    for (let attempt = 0; attempt < budget; attempt++) {
      const endpoint = this.pickEndpoint(tried);
      tried.add(endpoint.url);
      try {
        const result = await this.attempt<T>(method, params, endpoint.url);
        endpoint.cooldownUntil = 0; // recovered
        if (this.lastServedBy && this.lastServedBy !== endpoint.url) {
          this.log.warn('rpc endpoint switched', { from: this.lastServedBy, to: endpoint.url, method });
        }
        this.lastServedBy = endpoint.url;
        return result;
      } catch (err) {
        lastErr = err as RpcError;
        if (!lastErr.retryable) throw lastErr;
        // Retryable: park this endpoint so concurrent calls skip it too.
        endpoint.cooldownUntil = this.now() + ENDPOINT_COOLDOWN_MS;
        if (attempt === budget - 1) throw lastErr;
        // Only pay backoff once every endpoint has been tried; failing over to
        // a fresh host should be immediate.
        if (tried.size >= this.endpoints.length) {
          tried.clear();
          await delay(RETRY_BASE_MS * 2 ** Math.min(attempt, 4));
        }
      }
    }
    throw lastErr ?? new RpcError(`${method} failed`);
  }

  private async attempt<T>(method: string, params: unknown[], url: string): Promise<T> {
    const release = await this.semaphore.acquire();
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), this.timeoutMs);
    try {
      const res = await fetch(url, {
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

  /** Native SOL balance of an address, in lamports. */
  async getBalance(address: string, commitment: 'processed' | 'confirmed' | 'finalized' = 'confirmed'): Promise<bigint> {
    const result = await this.call<{ value: number }>('getBalance', [address, { commitment }]);
    return BigInt(result.value);
  }

  /** Confirmation status per signature; null entry when the signature is unknown. */
  async getSignatureStatuses(
    signatures: string[],
  ): Promise<Array<{ slot: number; confirmationStatus: 'processed' | 'confirmed' | 'finalized' | null; err: unknown } | null>> {
    if (signatures.length === 0) return [];
    const result = await this.call<{
      value: Array<{ slot: number; confirmationStatus: string | null; err: unknown } | null>;
    }>('getSignatureStatuses', [signatures, { searchTransactionHistory: false }]);
    return result.value.map((v) =>
      v ? { slot: v.slot, confirmationStatus: (v.confirmationStatus as 'processed' | 'confirmed' | 'finalized' | null) ?? null, err: v.err } : null,
    );
  }

  /** SPL token-account balance (raw amount + decimals), or null if the account does not exist. */
  async getTokenAccountBalance(
    ata: string,
    commitment: 'processed' | 'confirmed' | 'finalized' = 'confirmed',
  ): Promise<{ amount: bigint; decimals: number } | null> {
    try {
      const result = await this.call<{ value: { amount: string; decimals: number } }>('getTokenAccountBalance', [
        ata,
        { commitment },
      ]);
      return { amount: BigInt(result.value.amount), decimals: result.value.decimals };
    } catch (err) {
      // A non-existent token account is an expected "null", not a failure.
      if (err instanceof RpcError && /could not find account|Invalid param|not found/i.test(err.message)) return null;
      throw err;
    }
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
  ): Promise<Array<{ data: string; owner: string; lamports: number; executable: boolean } | null>> {
    if (pubkeys.length === 0) return [];
    const result = await this.call<{
      value: Array<{ data: [string, string]; owner: string; lamports: number; executable: boolean } | null>;
    }>('getMultipleAccounts', [pubkeys, { encoding: 'base64', commitment }]);
    return result.value.map((v) =>
      v ? { data: v.data[0], owner: v.owner, lamports: v.lamports, executable: v.executable } : null,
    );
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

  /**
   * Distinct token mints referenced by a transaction's token balances. Used to
   * recover the graduated mint from a migration signature (index-independent):
   * a pump.fun migration touches the token mint and WSOL, so the caller filters
   * out WSOL. Returns [] when the tx is not yet visible.
   */
  async getTransactionTokenMints(signature: string, commitment: 'confirmed' | 'finalized' = 'confirmed'): Promise<string[]> {
    const result = await this.call<{
      meta: { postTokenBalances?: Array<{ mint: string }>; preTokenBalances?: Array<{ mint: string }> } | null;
    } | null>('getTransaction', [signature, { commitment, maxSupportedTransactionVersion: 0, encoding: 'jsonParsed' }]);
    if (!result?.meta) return [];
    const balances = [...(result.meta.postTokenBalances ?? []), ...(result.meta.preTokenBalances ?? [])];
    return [...new Set(balances.map((b) => b.mint))];
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
