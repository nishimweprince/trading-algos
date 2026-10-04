/**
 * Transaction-error classification shared by the dry-run entry simulate.
 * (Previously part of the H4 sellability probe, removed 2026-09-28.)
 */

/**
 * The wallet itself is short of lamports (WSOL wrap / rent / fees) — the
 * simulate says nothing about the pool, only that this wallet cannot pay.
 */
/**
 * The simulating RPC node has not seen the migrate tx yet: the canonical pool
 * is still a System-owned (non-existent) account. Seen in dry-run as PumpSwap
 * ExtendAccount failing ConstraintOwner (2004) with Left = System program
 * ~30 ms after detection. A sent tx is ordered after the migration on-chain,
 * so this is a simulation-timing artifact, never a property of the trade.
 */
export function isPoolNotYetVisible(err: unknown, logs?: readonly string[]): boolean {
  if (logs?.some((l) => /Pool account not found|AccountNotInitialized/i.test(l))) return true;
  const text = errorSearchText(err);
  if (!/"Custom":\s*(2004|3012)/.test(text)) return false;
  const l = logs ?? [];
  const left = l.findIndex((x) => /Program log: Left:/.test(x));
  return left >= 0 && /11111111111111111111111111111111/.test(l[left + 1] ?? '');
}

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
