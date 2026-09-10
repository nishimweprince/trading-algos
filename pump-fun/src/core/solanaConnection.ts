import { Connection, type Commitment } from '@solana/web3.js';
import { registerSecret } from './logger.ts';

/**
 * Shared web3.js Connection factory (Supanode support).
 *
 * Header-auth providers (Supanode) require an `x-token` (or
 * `Authorization: Bearer`) header on every HTTP request, but `new
 * Connection(url, 'confirmed')` sends none — so sends/simulations against
 * such an endpoint fail with HTTP 401. web3.js supports this natively via
 * `httpHeaders`; this factory threads it through and registers the values
 * as log secrets. Key-in-URL providers (Helius `?api-key=`) ignore extra
 * headers, so passing one headers object everywhere is safe.
 */
export function createConnection(
  httpUrl: string,
  opts: { commitment?: Commitment; headers?: Record<string, string> } = {},
): Connection {
  const headers = opts.headers ?? {};
  for (const value of Object.values(headers)) registerSecret(value);
  if (Object.keys(headers).length === 0) {
    return new Connection(httpUrl, opts.commitment ?? 'confirmed');
  }
  return new Connection(httpUrl, {
    commitment: opts.commitment ?? 'confirmed',
    httpHeaders: { ...headers },
  });
}
