export async function sleepMs(ms: number): Promise<void> {
  await new Promise((r) => setTimeout(r, ms));
}

export async function withOneRetry<T>(
  fn: () => Promise<T>,
  delayMs: number,
  onRetry?: (err: unknown) => void,
): Promise<T> {
  try {
    return await fn();
  } catch (e) {
    onRetry?.(e);
    await sleepMs(delayMs);
    return await fn();
  }
}
