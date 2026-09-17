/**
 * Bound a promise to `ms`. web3.js Connection calls have no default fetch
 * timeout, so a stalled RPC would otherwise hang an entry or exit forever.
 */
export class TimeoutError extends Error {
  override name = 'TimeoutError';
}

export function withTimeout<T>(p: Promise<T>, ms: number, label: string): Promise<T> {
  if (!(ms > 0)) return p;
  let timer: NodeJS.Timeout;
  const gate = new Promise<never>((_, reject) => {
    timer = setTimeout(() => reject(new TimeoutError(`${label} timed out after ${ms}ms`)), ms);
    timer.unref?.();
  });
  return Promise.race([p, gate]).finally(() => clearTimeout(timer)) as Promise<T>;
}
