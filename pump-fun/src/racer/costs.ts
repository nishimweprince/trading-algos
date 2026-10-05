/**
 * Measured live execution cost of a buy (pure), from a getTransaction result
 * (encoding: json): the network fee (base + priority) plus every lamport
 * increase on an account that is neither the payer nor a token account.
 *
 * That catches tips to any landing service (Jito, Helius Sender, Nozomi,
 * 0slot, …) without a list of their accounts, because pool and protocol
 * fees are paid in WSOL — token accounts — and are already in the paper's
 * tiered fee model. Rent for program PDAs created on a first trade (e.g.
 * pump's volume accumulator) also counts: it is a real, non-refundable cost.
 * New token accounts (ATA rent, refundable on close) do not.
 */
export interface RawTx {
  transaction?: { message?: { accountKeys?: unknown[] } };
  meta?: {
    err?: unknown;
    fee?: number;
    preBalances?: number[];
    postBalances?: number[];
    preTokenBalances?: Array<{ accountIndex?: number }>;
    postTokenBalances?: Array<{ accountIndex?: number }>;
  } | null;
}

export function txExecutionCostSol(tx: RawTx | null | undefined): number | null {
  const meta = tx?.meta;
  if (!meta || meta.err || !Array.isArray(meta.preBalances) || !Array.isArray(meta.postBalances)) return null;
  const tokenIdx = new Set<number>();
  for (const b of [...(meta.preTokenBalances ?? []), ...(meta.postTokenBalances ?? [])]) {
    if (typeof b.accountIndex === 'number') tokenIdx.add(b.accountIndex);
  }
  let tips = 0;
  for (let i = 1; i < meta.postBalances.length; i++) {
    if (tokenIdx.has(i)) continue;
    const d = (meta.postBalances[i] ?? 0) - (meta.preBalances[i] ?? 0);
    if (d > 0) tips += d;
  }
  return ((meta.fee ?? 0) + tips) / 1e9;
}

export function median(values: readonly number[]): number | null {
  if (values.length === 0) return null;
  const s = [...values].sort((a, b) => a - b);
  const m = s.length >> 1;
  return s.length % 2 ? s[m]! : (s[m - 1]! + s[m]!) / 2;
}
