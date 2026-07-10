import { sleep } from './sleep.js';

export function retryDelay(attempt: number, baseDelayMs: number): number {
  const jitter = Math.floor(Math.random() * baseDelayMs);
  return baseDelayMs * 2 ** Math.max(0, attempt - 1) + jitter;
}

export async function waitForRetry(attempt: number, baseDelayMs: number): Promise<void> {
  await sleep(retryDelay(attempt, baseDelayMs));
}
