/**
 * Migration-racer verification (spec §7). Read-only: RPC reads only, no
 * signing, no sends.
 *
 * Samples recent pump.fun `migrate` transactions via the migration authority
 * shortcut (every Migrate/MigrateV2 includes PUMP_FUN_MIGRATION_AUTHORITY, so
 * its signature history is dense in migrations), ranks caller wallets, and
 * measures what — if anything — the caller receives (R) by diffing pre/post
 * balances in each migrate transaction.
 *
 * Pure analysis (isMigrateTx, callerSolDelta, callerTokenDeltas,
 * graduatedMint, rankCallers) is unit-tested; sampling/probing hit the network.
 */
import { hasMigrateLog } from '../detector/migrateLog.ts';
import { LAMPORTS_PER_SOL, PUMP_FUN_MIGRATION_AUTHORITY, WSOL_MINT } from '../core/constants.ts';
import type { ParsedTx, RpcClient } from '../core/rpc.ts';

/** ParsedTx as jsonParsed returns it, including logs (the core subset omits them). */
export type ParsedTxWithLogs = Omit<ParsedTx, 'meta'> & {
  meta: NonNullable<ParsedTx['meta']> & { logMessages?: string[] };
};

/** pump.fun buy instruction log, anchored like the migrate matcher. */
export const BUY_LOG = /Instruction:\s*Buy(?:V\d+)?\s*$/im;

export function hasBuyLog(logs: readonly string[]): boolean {
  return logs.some((l) => BUY_LOG.test(l));
}

/** Incentive-claim programs name their instruction differently; match loosely and report raw lines. */
export function incentiveLogLines(logs: readonly string[]): string[] {
  return logs.filter((l) => /incentive/i.test(l));
}

export function isMigrateTx(tx: ParsedTxWithLogs): boolean {
  return tx.meta !== null && !tx.meta.err && hasMigrateLog(tx.meta.logMessages ?? []);
}

/** Fee payer is always account index 0. */
export function callerOf(tx: ParsedTx): string | null {
  return tx.transaction.message.accountKeys[0]?.pubkey ?? null;
}

export interface CallerSol {
  /** post - pre for the fee payer, lamports. Negative when the caller only paid costs. */
  deltaLamports: number;
  /** Transaction fee, lamports. */
  feeLamports: number;
  /** delta + fee: what the caller received before paying the tx fee. Positive R shows up here. */
  netOfFeeLamports: number;
}

export function callerSolDelta(tx: ParsedTx): CallerSol | null {
  const meta = tx.meta;
  if (!meta || meta.preBalances.length === 0 || meta.postBalances.length === 0) return null;
  const deltaLamports = meta.postBalances[0]! - meta.preBalances[0]!;
  const feeLamports = meta.fee ?? 0;
  return { deltaLamports, feeLamports, netOfFeeLamports: deltaLamports + feeLamports };
}

export interface CallerTokenDelta {
  mint: string;
  /** Token-account owner when the balances name one; null rows are unattributed, never assumed. */
  owner: string | null;
  deltaRaw: string;
  decimals: number;
}

/**
 * Token-balance deltas for accounts owned by `caller`, plus unattributed
 * (owner-less) rows. Rows owned by anyone else are excluded. Covers accounts
 * created, changed, and closed within the transaction.
 */
export function callerTokenDeltas(tx: ParsedTx, caller: string): CallerTokenDelta[] {
  const meta = tx.meta;
  if (!meta) return [];
  const pre = new Map<string, { amount: bigint; decimals: number; owner: string | undefined }>();
  for (const b of meta.preTokenBalances ?? []) {
    pre.set(`${b.accountIndex}:${b.mint}`, {
      amount: BigInt(b.uiTokenAmount.amount),
      decimals: b.uiTokenAmount.decimals,
      owner: b.owner,
    });
  }
  const out: CallerTokenDelta[] = [];
  const seen = new Set<string>();
  for (const b of meta.postTokenBalances ?? []) {
    const key = `${b.accountIndex}:${b.mint}`;
    seen.add(key);
    const p = pre.get(key);
    const delta = BigInt(b.uiTokenAmount.amount) - (p?.amount ?? 0n);
    if (delta === 0n) continue;
    const owner = b.owner ?? p?.owner ?? null;
    if (owner !== null && owner !== caller) continue;
    out.push({ mint: b.mint, owner, deltaRaw: delta.toString(), decimals: b.uiTokenAmount.decimals });
  }
  for (const [key, p] of pre) {
    if (seen.has(key)) continue;
    const delta = -p.amount;
    if (delta === 0n) continue;
    if (p.owner !== undefined && p.owner !== caller) continue;
    out.push({
      mint: key.slice(key.indexOf(':') + 1),
      owner: p.owner ?? null,
      deltaRaw: delta.toString(),
      decimals: p.decimals,
    });
  }
  return out;
}

/**
 * The graduated mint: the single non-WSOL mint in the token balances.
 * Returns null when ambiguous (zero or 2+ candidates) — callers must check
 * candidateMints instead of trusting a guess.
 */
export function graduatedMint(tx: ParsedTx): { mint: string | null; candidateMints: string[] } {
  const meta = tx.meta;
  if (!meta) return { mint: null, candidateMints: [] };
  const candidates = [
    ...new Set(
      [...(meta.preTokenBalances ?? []), ...(meta.postTokenBalances ?? [])]
        .map((b) => b.mint)
        .filter((m) => m !== WSOL_MINT),
    ),
  ];
  return { mint: candidates.length === 1 ? candidates[0]! : null, candidateMints: candidates };
}

export interface MigrateSample {
  signature: string;
  slot: number;
  blockTime: number | null;
  caller: string;
  sol: CallerSol;
  tokenDeltas: CallerTokenDelta[];
  graduatedMint: string | null;
  candidateMints: string[];
}

export function summarizeMigrate(signature: string, tx: ParsedTxWithLogs): MigrateSample | null {
  if (!isMigrateTx(tx)) return null;
  const caller = callerOf(tx);
  const sol = callerSolDelta(tx);
  if (!caller || !sol) return null;
  const { mint, candidateMints } = graduatedMint(tx);
  return {
    signature,
    slot: tx.slot,
    blockTime: tx.blockTime,
    caller,
    sol,
    tokenDeltas: callerTokenDeltas(tx, caller),
    graduatedMint: mint,
    candidateMints,
  };
}

export interface CallerStats {
  caller: string;
  migrates: number;
  totalSolDeltaLamports: number;
  totalFeeLamports: number;
  totalNetOfFeeLamports: number;
  /** Migrates where the caller ended net-positive before fees (caller-reward support). */
  netPositiveCount: number;
  /** Token gains on caller-owned accounts, summed per mint (raw units). */
  tokenGains: Array<{ mint: string; deltaRaw: string; decimals: number }>;
  unattributedTokenRows: number;
}

export function rankCallers(samples: MigrateSample[]): CallerStats[] {
  const by = new Map<string, CallerStats & { gains: Map<string, { delta: bigint; decimals: number }> }>();
  for (const s of samples) {
    let c = by.get(s.caller);
    if (!c) {
      c = {
        caller: s.caller,
        migrates: 0,
        totalSolDeltaLamports: 0,
        totalFeeLamports: 0,
        totalNetOfFeeLamports: 0,
        netPositiveCount: 0,
        tokenGains: [],
        unattributedTokenRows: 0,
        gains: new Map(),
      };
      by.set(s.caller, c);
    }
    c.migrates += 1;
    c.totalSolDeltaLamports += s.sol.deltaLamports;
    c.totalFeeLamports += s.sol.feeLamports;
    c.totalNetOfFeeLamports += s.sol.netOfFeeLamports;
    if (s.sol.netOfFeeLamports > 0) c.netPositiveCount += 1;
    for (const t of s.tokenDeltas) {
      if (t.owner === null) {
        c.unattributedTokenRows += 1;
        continue;
      }
      const g = c.gains.get(t.mint) ?? { delta: 0n, decimals: t.decimals };
      g.delta += BigInt(t.deltaRaw);
      c.gains.set(t.mint, g);
    }
  }
  return [...by.values()]
    .map(({ gains, ...c }) => ({
      ...c,
      tokenGains: [...gains.entries()].map(([mint, g]) => ({
        mint,
        deltaRaw: g.delta.toString(),
        decimals: g.decimals,
      })),
    }))
    .sort((a, b) => b.migrates - a.migrates);
}

async function mapPool<T, R>(items: T[], size: number, fn: (t: T) => Promise<R>): Promise<R[]> {
  const out: R[] = new Array(items.length);
  let i = 0;
  const workers = Array.from({ length: Math.min(Math.max(size, 1), items.length) }, async () => {
    while (i < items.length) {
      const j = i++;
      out[j] = await fn(items[j]!);
    }
  });
  await Promise.all(workers);
  return out;
}

export interface SamplerOptions {
  maxMigrates: number;
  pageLimit?: number;
  maxPages?: number;
  concurrency?: number;
  onProgress?: (migrates: number, scanned: number) => void;
}

export interface SampleResult {
  samples: MigrateSample[];
  stats: {
    signaturesScanned: number;
    fetched: number;
    fetchFailed: number;
    failedTx: number;
    nonMigrate: number;
    pages: number;
  };
}

/** Page the migration authority's history until maxMigrates confirmed migrates are collected. */
export async function sampleMigrates(rpc: RpcClient, opts: SamplerOptions): Promise<SampleResult> {
  const pageLimit = opts.pageLimit ?? 1000;
  const maxPages = opts.maxPages ?? 25;
  const concurrency = opts.concurrency ?? 4;
  const samples: MigrateSample[] = [];
  const stats = { signaturesScanned: 0, fetched: 0, fetchFailed: 0, failedTx: 0, nonMigrate: 0, pages: 0 };
  let before: string | undefined;
  for (let page = 0; page < maxPages && samples.length < opts.maxMigrates; page++) {
    const sigs = await rpc.getSignaturesForAddress(
      PUMP_FUN_MIGRATION_AUTHORITY,
      before === undefined ? { limit: pageLimit } : { limit: pageLimit, before },
    );
    if (sigs.length === 0) break;
    stats.pages += 1;
    stats.signaturesScanned += sigs.length;
    before = sigs[sigs.length - 1]!.signature;
    const fresh = sigs.filter((s) => !s.err);
    stats.failedTx += sigs.length - fresh.length;
    const txs = await mapPool(fresh, concurrency, async (s) => {
      try {
        return { signature: s.signature, tx: (await rpc.getParsedTransaction(s.signature)) as ParsedTxWithLogs | null };
      } catch {
        return { signature: s.signature, tx: null as null, failed: true as const };
      }
    });
    for (const r of txs) {
      if (r.tx === null) {
        stats.fetchFailed += 1;
        continue;
      }
      stats.fetched += 1;
      const sample = summarizeMigrate(r.signature, r.tx);
      if (!sample) {
        stats.nonMigrate += 1;
        continue;
      }
      if (samples.length < opts.maxMigrates) samples.push(sample);
    }
    opts.onProgress?.(samples.length, stats.signaturesScanned);
    if (sigs.length < pageLimit) break;
  }
  return { samples, stats };
}

export interface CallerProbe {
  caller: string;
  migratesWon: number;
  historyScanned: number;
  /** Buys by the caller on a migrated mint within `slotWindow` slots before its migrate. */
  completionBuys: Array<{ signature: string; slot: number; mint: string; slotsBeforeMigrate: number }>;
  /** Buys by the caller on a migrated mint within `slotWindow` slots after its migrate. */
  postMigrateBuys: Array<{ signature: string; slot: number; mint: string; slotsAfterMigrate: number }>;
  incentiveClaims: Array<{ signature: string; slot: number; lines: string[] }>;
  hadAnyBuy: boolean;
}

export interface ProbeOptions {
  depth?: number;
  slotWindow?: number;
  concurrency?: number;
}

/**
 * Bounded caller history probe (spec §7 steps 3–5): one history page per
 * caller, looking for completion buys, post-migration buys, and incentive
 * claims. Best-effort: bundle membership is not observable via RPC, so
 * same-slot proximity is reported, not bundle proof.
 */
export async function probeCaller(
  rpc: RpcClient,
  caller: string,
  migrates: MigrateSample[],
  opts: ProbeOptions = {},
): Promise<CallerProbe> {
  const depth = Math.min(opts.depth ?? 100, 1000);
  const slotWindow = opts.slotWindow ?? 150;
  const concurrency = opts.concurrency ?? 4;
  const probe: CallerProbe = {
    caller,
    migratesWon: migrates.length,
    historyScanned: 0,
    completionBuys: [],
    postMigrateBuys: [],
    incentiveClaims: [],
    hadAnyBuy: false,
  };
  const byMint = new Map<string, MigrateSample[]>();
  for (const m of migrates) {
    if (!m.graduatedMint) continue;
    const list = byMint.get(m.graduatedMint) ?? [];
    list.push(m);
    byMint.set(m.graduatedMint, list);
  }
  const sigs = await rpc.getSignaturesForAddress(caller, { limit: depth });
  const fresh = sigs.filter((s) => !s.err);
  probe.historyScanned = fresh.length;
  const txs = await mapPool(fresh, concurrency, async (s) => {
    try {
      return { signature: s.signature, tx: (await rpc.getParsedTransaction(s.signature)) as ParsedTxWithLogs | null };
    } catch {
      return { signature: s.signature, tx: null as null };
    }
  });
  for (const r of txs) {
    if (!r.tx?.meta || r.tx.meta.err) continue;
    const logs = r.tx.meta.logMessages ?? [];
    const incentives = incentiveLogLines(logs);
    if (incentives.length > 0) {
      probe.incentiveClaims.push({ signature: r.signature, slot: r.tx.slot, lines: incentives.slice(0, 3) });
    }
    if (!hasBuyLog(logs)) continue;
    probe.hadAnyBuy = true;
    const { mint } = graduatedMint(r.tx);
    if (!mint) continue;
    for (const m of byMint.get(mint) ?? []) {
      const gap = m.slot - r.tx.slot;
      if (gap >= 0 && gap <= slotWindow) {
        probe.completionBuys.push({ signature: r.signature, slot: r.tx.slot, mint, slotsBeforeMigrate: gap });
      } else if (gap < 0 && -gap <= slotWindow) {
        probe.postMigrateBuys.push({ signature: r.signature, slot: r.tx.slot, mint, slotsAfterMigrate: -gap });
      }
    }
  }
  return probe;
}

export interface VerificationFindings {
  generatedAt: string;
  params: { maxMigrates: number; probeTop: number; probeDepth: number; slotWindow: number };
  stats: SampleResult['stats'];
  sampleCount: number;
  callersRanked: CallerStats[];
  probes: CallerProbe[];
}

export function lamportsToSol(lamports: number): number {
  return lamports / LAMPORTS_PER_SOL;
}

export function renderMarkdown(f: VerificationFindings): string {
  const lines: string[] = [];
  lines.push('# Migration-racer verification (spec §7)');
  lines.push('');
  lines.push(`Generated: ${f.generatedAt}`);
  lines.push(
    `Params: maxMigrates=${f.params.maxMigrates} probeTop=${f.params.probeTop} probeDepth=${f.params.probeDepth} slotWindow=${f.params.slotWindow}`,
  );
  lines.push('');
  lines.push('## Sample');
  lines.push('');
  lines.push(`- Migrates confirmed: ${f.sampleCount}`);
  lines.push(`- Signatures scanned: ${f.stats.signaturesScanned} over ${f.stats.pages} page(s)`);
  lines.push(`- Fetched: ${f.stats.fetched}, fetch failed: ${f.stats.fetchFailed}`);
  lines.push(`- Failed txs skipped: ${f.stats.failedTx}, non-migrate touching authority: ${f.stats.nonMigrate}`);
  lines.push('');
  lines.push('## Caller ranking (top 10)');
  lines.push('');
  lines.push('| Caller | Migrates | Net of fee (SOL) | Net-positive | Token gains (caller-owned) |');
  lines.push('| --- | --- | --- | --- | --- |');
  for (const c of f.callersRanked.slice(0, 10)) {
    const gains =
      c.tokenGains.length > 0
        ? c.tokenGains.map((g) => `${g.mint.slice(0, 8)}… ${g.deltaRaw}`).join('; ')
        : 'none';
    lines.push(
      `| ${c.caller} | ${c.migrates} | ${lamportsToSol(c.totalNetOfFeeLamports).toFixed(6)} | ${c.netPositiveCount}/${c.migrates} | ${gains} |`,
    );
  }
  lines.push('');
  lines.push('## Caller probes (spec §7 steps 3–5)');
  lines.push('');
  if (f.probes.length === 0) {
    lines.push('No probes run (probeTop=0).');
  }
  for (const p of f.probes) {
    lines.push(`### ${p.caller} (won ${p.migratesWon}, history scanned ${p.historyScanned})`);
    lines.push('');
    lines.push(`- Any buy activity: ${p.hadAnyBuy ? 'yes' : 'no'}`);
    lines.push(`- Completion buys (≤${f.params.slotWindow} slots before): ${p.completionBuys.length}`);
    for (const b of p.completionBuys.slice(0, 10)) {
      lines.push(`  - ${b.mint} ${b.slotsBeforeMigrate} slots before (${b.signature.slice(0, 16)}…)`);
    }
    lines.push(`- Post-migrate buys (≤${f.params.slotWindow} slots after): ${p.postMigrateBuys.length}`);
    for (const b of p.postMigrateBuys.slice(0, 10)) {
      lines.push(`  - ${b.mint} ${b.slotsAfterMigrate} slots after (${b.signature.slice(0, 16)}…)`);
    }
    lines.push(`- Incentive-claim txs: ${p.incentiveClaims.length}`);
    for (const c of p.incentiveClaims.slice(0, 5)) {
      lines.push(`  - slot ${c.slot}: ${c.lines.join(' / ').slice(0, 160)}`);
    }
    lines.push('');
  }
  lines.push('## Hypothesis scoreboard (raw counts — see analysis before concluding)');
  lines.push('');
  const netPos = f.callersRanked.reduce((n, c) => n + c.netPositiveCount, 0);
  const withTokenGains = f.callersRanked.filter((c) =>
    c.tokenGains.some((g) => BigInt(g.deltaRaw) > 0n),
  ).length;
  const completion = f.probes.reduce((n, p) => n + p.completionBuys.length, 0);
  const post = f.probes.reduce((n, p) => n + p.postMigrateBuys.length, 0);
  const claims = f.probes.reduce((n, p) => n + p.incentiveClaims.length, 0);
  lines.push(`- Migrates with caller net-of-fee > 0: ${netPos}/${f.sampleCount} (caller-reward support)`);
  lines.push(`- Callers with caller-owned token gains: ${withTokenGains} (LP/payout-token support)`);
  lines.push(`- Completion buys found in probes: ${completion} (push-over-the-line support)`);
  lines.push(`- Post-migrate buys found in probes: ${post} (first-position support)`);
  lines.push(`- Incentive claims found in probes: ${claims} (volume-incentive support)`);
  lines.push('');
  lines.push('Caveats: RPC cannot prove bundle membership — slot proximity is reported, not bundles.');
  lines.push('Fee-payer attribution assumes the migrate caller pays the transaction fee.');
  return lines.join('\n');
}
