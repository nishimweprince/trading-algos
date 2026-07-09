import type { TxSender, TxSendResult } from './broadcaster.ts';
import { registerSecret } from '../core/logger.ts';

interface JitoRpcResponse<T> {
  result?: T;
  error?: { code: number; message: string };
}

export class JitoTxSender implements TxSender {
  readonly name = 'jito';
  private readonly baseUrl: string;
  private readonly authToken: string | undefined;
  private tipAccounts: string[] = [];
  private tipAccountsFetchedAtMs = 0;

  constructor(opts: { blockEngineUrl: string; authToken?: string; tipAccountTtlMs?: number }) {
    this.baseUrl = opts.blockEngineUrl.replace(/\/+$/, '');
    this.authToken = opts.authToken;
    registerSecret(this.baseUrl);
    if (this.authToken) registerSecret(this.authToken);
  }

  async simulate(): Promise<{ err: unknown; logs: string[] }> {
    throw new Error('Jito sender does not support local simulation; configure an RPC simulator');
  }

  async send(txBytes: Uint8Array): Promise<TxSendResult> {
    const encoded = Buffer.from(txBytes).toString('base64');
    const res = await this.post<string>('/api/v1/transactions', 'sendTransaction', [
      encoded,
      { encoding: 'base64' },
    ]);
    return { signature: res.result, ...(res.bundleId ? { bundleId: res.bundleId } : {}) };
  }

  async getTipAccount(ttlMs = 60_000): Promise<string | undefined> {
    const now = Date.now();
    if (this.tipAccounts.length > 0 && now - this.tipAccountsFetchedAtMs < ttlMs) {
      return randomItem(this.tipAccounts);
    }
    const res = await this.post<string[]>('/api/v1/getTipAccounts', 'getTipAccounts', []);
    this.tipAccounts = res.result;
    this.tipAccountsFetchedAtMs = now;
    return randomItem(this.tipAccounts);
  }

  async getInflightBundleStatus(bundleId: string): Promise<string | null> {
    const res = await this.post<{ value?: Array<{ bundle_id: string; status: string }> | null }>(
      '/api/v1/getInflightBundleStatuses',
      'getInflightBundleStatuses',
      [[bundleId]],
    );
    return res.result.value?.find((v) => v.bundle_id === bundleId)?.status ?? null;
  }

  private async post<T>(
    path: string,
    method: string,
    params: unknown[],
  ): Promise<{ result: T; bundleId?: string }> {
    const headers: Record<string, string> = { 'content-type': 'application/json' };
    if (this.authToken) headers['x-jito-auth'] = this.authToken;
    const res = await fetch(`${this.baseUrl}${path}`, {
      method: 'POST',
      headers,
      body: JSON.stringify({ jsonrpc: '2.0', id: 1, method, params }),
    });
    if (!res.ok) throw new Error(`Jito ${method} HTTP ${res.status}`);
    const body = (await res.json()) as JitoRpcResponse<T>;
    if (body.error) throw new Error(`Jito ${method}: ${body.error.message} (${body.error.code})`);
    if (body.result === undefined) throw new Error(`Jito ${method}: missing result`);
    const bundleId = res.headers.get('x-bundle-id') ?? undefined;
    return { result: body.result, ...(bundleId ? { bundleId } : {}) };
  }
}

function randomItem<T>(items: T[]): T | undefined {
  if (items.length === 0) return undefined;
  return items[Math.floor(Math.random() * items.length)];
}
