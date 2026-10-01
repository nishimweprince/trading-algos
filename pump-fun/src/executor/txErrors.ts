/**
 * Transaction-error classification shared by the dry-run entry simulate.
 * (Previously part of the H4 sellability probe, removed 2026-09-28.)
 */

/**
 * The wallet itself is short of lamports (WSOL wrap / rent / fees) — the
 * simulate says nothing about the pool, only that this wallet cannot pay.
 */
export function isLamportShortfall(err: unknown, logs?: readonly string[]): boolean {
  if (logs?.some((l) => /insufficient lamports/i.test(l))) return true;
  return /InsufficientFunds|insufficient lamports/i.test(errorSearchText(err));
}

/** Flattened text of an error (message, stack, cause, or JSON) for pattern matching. */
function errorSearchText(err: unknown): string {
  const parts: string[] = [];
  const visit = (e: unknown, depth: number): void => {
    if (e == null || depth > 3) return;
    if (e instanceof Error) {
      parts.push(e.name, e.message);
      if (e.stack) parts.push(e.stack);
      visit((e as { cause?: unknown }).cause, depth + 1);
      return;
    }
    if (typeof e === 'string') {
      parts.push(e);
      return;
    }
    try {
      parts.push(JSON.stringify(e));
    } catch {
      parts.push(String(e));
    }
  };
  visit(err, 0);
  return parts.join(' ');
}
