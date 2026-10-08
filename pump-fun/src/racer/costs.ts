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
  const p = txExecutionCostParts(tx);
  // Same op order as the historic inline math — bit-identical totals.
  return p ? (p.feeLamports + p.tipLamports) / 1e9 : null;
}

export interface TxCostPartsLamports {
  /** meta.fee: base + priority, route-independent. */
  feeLamports: number;
  /** Tips to any landing service (route-dependent). */
  tipLamports: number;
}

/** Fee/tip split of txExecutionCostSol. Integer lamports: the sum is exact. */
export function txExecutionCostParts(tx: RawTx | null | undefined): TxCostPartsLamports | null {
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
  return { feeLamports: meta.fee ?? 0, tipLamports: tips };
}

export function median(values: readonly number[]): number | null {
  if (values.length === 0) return null;
  const s = [...values].sort((a, b) => a - b);
  const m = s.length >> 1;
  return s.length % 2 ? s[m]! : (s[m - 1]! + s[m]!) / 2;
}

export interface BuyCostSample {
  buyFeeSol: number;
  buyTipSol: number;
  /** Buyers whose txs were readable. */
  n: number;
}

/**
 * Median buy-side execution split over a buyer sample: network fees (ours to
 * keep — route-independent) and tips (theirs — see routeTipLamports). Shared
 * by the worker (live measurement) and the backfill CLI (patient retries).
 */
export async function measureBuyCostParts(
  fetchTx: (sig: string) => Promise<RawTx | null>,
  signatures: readonly string[],
  sampleN: number,
  opts: { attempts?: number; delayMs?: number; sleep?: (ms: number) => Promise<void> } = {},
): Promise<BuyCostSample | null> {
  const sample = signatures.slice(0, Math.max(0, sampleN));
  if (sample.length === 0) return null;
  const attempts = opts.attempts ?? 3;
  const delayMs = opts.delayMs ?? 1_500;
  const sleep = opts.sleep ?? ((ms) => new Promise((r) => setTimeout(r, ms)));
  const fees: number[] = [];
  const tips: number[] = [];
  for (let attempt = 0; attempt < attempts && fees.length === 0; attempt++) {
    if (attempt > 0) await sleep(delayMs);
    const got = await Promise.all(sample.map((s) => fetchTx(s).catch(() => null)));
    for (const tx of got) {
      const c = txExecutionCostParts(tx);
      if (c !== null) {
        fees.push(c.feeLamports / 1e9);
        tips.push(c.tipLamports / 1e9);
      }
    }
  }
  const fee = median(fees);
  const tip = median(tips);
  if (fee === null || tip === null) return null;
  return { buyFeeSol: fee, buyTipSol: tip, n: fees.length };
}

/** SWQoS protocol minimum tip per tx (see HeliusSenderConfig comments). */
export const SWQOS_MIN_TIP_LAMPORTS = 5_000;

export interface SenderTipConfig {
  enabled: boolean;
  swqosOnly: boolean;
  exitSwqosOnly: boolean;
  minTipLamports: number;
}

/**
 * Tip OUR route would pay per tx, in lamports. Disabled sender = 0 (priority
 * fees only, already in the measured fee). Max-mode buys use minTipLamports
 * as a LOWER bound — the live percentile-of-floor tip is unknowable post-hoc.
 *
 * Pairing caveat: our tips priced against the POPULATION fill-p is an
 * optimistic mix when our tip undercuts the population's (cheaper sends land
 * worse). Route scenarios are cost comparisons, not expectancy, until a
 * tip→fill curve exists (pool_buy_attempts.tip_sol feeds it).
 */
export function routeTipLamports(sender: SenderTipConfig, side: 'buy' | 'sell'): number {
  if (!sender.enabled) return 0;
  if (side === 'sell' && sender.exitSwqosOnly && !sender.swqosOnly) return SWQOS_MIN_TIP_LAMPORTS;
  return sender.minTipLamports;
}
