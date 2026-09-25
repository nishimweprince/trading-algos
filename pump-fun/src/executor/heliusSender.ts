import type { TxSender, TxSendResult } from './broadcaster.ts';
import { registerSecret } from '../core/logger.ts';

/**
 * Helius Sender send path (work plan 2026-09-25 P4.1, F13; [R3][R10]).
 *
 * Sender fans a transaction out over staked (SWQoS) connections and, in "Max"
 * mode, Jito in parallel — the plain `sendRawTransaction` to our own RPCs
 * reaches neither. Every submission must carry a SOL tip to one of the
 * designated tip accounts below AND a compute-unit price instruction; the
 * Executor adds both (fees.ts buildFeePlan + assemble.ts).
 *
 * Tiers (docs, verified 2026-09-25):
 *   swqosOnly (`?swqos_only=true`)  min tip 0.000005 SOL — staked routes only
 *   Max (default endpoint)          min tip 0.001 SOL    — staked + Jito
 * At a 0.04 SOL position a Max tip on both legs is ~5 % of notional, so
 * swqosOnly is the default; Max is opt-in.
 *
 * `simulate` throws, like the Jito sender: the primary RPC stays the simulator.
 */
export const HELIUS_SENDER_TIP_ACCOUNTS: readonly string[] = [
  '4ACfpUFoaSD9bfPdeu6DBt89gB6ENTeHBXCAi87NhDEE',
  'D2L6yPZ2FmmmTKPgzaMKdhu6EWZcTpLy1Vhx8uvZe7NZ',
  '9bnz4RShgq1hAnLnZbP8kbgBg1kEmcJBYQq3gQbmnSta',
  '5VY91ws6B2hMmBFRsXkoAAdsPHBJwRfBht4DXox3xkwn',
  '2nyhqdwKcJZR2vcqCyrYsaPVdAnFoJjiksCXJ7hfEYgD',
  '2q5pghRs6arqVjRvT5gfgWfWcHWmw1ZuCzphgd5KfWGJ',
  'wyvPkWjVZz1M8fHQnMMCDTQDbkManefNNhweYk5WkcF',
  '3KCKozbAaF75qEU33jtzozcJ29yJuaLJTy2jFdzUY8bT',
  '4vieeGHPYPG2MmyPRcYjdiDmmhN3ww7hsFNap8pVN3Ey',
  '4TQLFNWK8AovT1gFvda5jfw2oJeRMKEmw7aH6MGBJ3or',
];

/** Documented minimum tips, lamports. */
export const SENDER_MIN_TIP_LAMPORTS = { swqosOnly: 5_000, max: 1_000_000 } as const;

export function heliusSenderUrl(base: string, opts: { swqosOnly: boolean; apiKey?: string | undefined }): string {
  const u = new URL(base);
  if (opts.swqosOnly) u.searchParams.set('swqos_only', 'true');
  if (opts.apiKey) u.searchParams.set('api-key', opts.apiKey);
  return u.toString();
}

export function randomSenderTipAccount(rng: () => number = Math.random): string {
  return HELIUS_SENDER_TIP_ACCOUNTS[Math.floor(rng() * HELIUS_SENDER_TIP_ACCOUNTS.length)]!;
}

export class HeliusSenderTxSender implements TxSender {
  readonly name = 'helius-sender';
  private readonly url: string;
  private readonly fetchImpl: typeof fetch;

  constructor(opts: { url: string; swqosOnly: boolean; apiKey?: string | undefined; fetchImpl?: typeof fetch }) {
    this.url = heliusSenderUrl(opts.url, { swqosOnly: opts.swqosOnly, apiKey: opts.apiKey });
    if (opts.apiKey) registerSecret(opts.apiKey);
    this.fetchImpl = opts.fetchImpl ?? fetch;
  }

  async simulate(): Promise<{ err: unknown; logs: string[] }> {
    throw new Error('Helius Sender does not simulate; configure an RPC simulator');
  }

  async send(txBytes: Uint8Array): Promise<TxSendResult> {
    const res = await this.fetchImpl(this.url, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({
        jsonrpc: '2.0',
        id: Date.now(),
        method: 'sendTransaction',
        params: [Buffer.from(txBytes).toString('base64'), { encoding: 'base64', skipPreflight: true, maxRetries: 0 }],
      }),
    });
    if (!res.ok) throw new Error(`Helius Sender HTTP ${res.status}`);
    const body = (await res.json()) as { result?: string; error?: { code: number; message: string } };
    if (body.error) throw new Error(`Helius Sender: ${body.error.message} (${body.error.code})`);
    if (!body.result) throw new Error('Helius Sender: missing signature');
    return { signature: body.result };
  }
}
