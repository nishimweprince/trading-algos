/**
 * Migrate-transaction parsing for the racer (pure).
 *
 * The racer's stream delivers every transaction that references pump.fun's
 * migration authority, failed ones included. From each one this extracts:
 * who called it, which mint, whether it landed, and what else the caller
 * packed into the same transaction — a completion buy on the curve before
 * the migrate (push-over-the-line) and/or a PumpSwap buy after it (first
 * position). From a landed one it also prices the new pool twice:
 *
 *   reach — the pool after the whole transaction: the best price anyone who
 *           did NOT call migrate can get.
 *   open  — the pool as migrate created it, before the caller's bundled
 *           buy: what "be the migration" gets. Backed out of the bundled
 *           buy along the constant-product curve (E0 = E1·B1/B0). Unknown
 *           (null) when one tx both completes the curve and buys the pool,
 *           because the caller's token gain then mixes both legs.
 */
import { LAMPORTS_PER_SOL, PROGRAM_IDS, WSOL_MINT } from '../core/constants.ts';
import { base58Encode } from '../core/base58.ts';
import type { TxTokenBalance } from '../core/types.ts';
import { MIGRATE_LOG } from '../detector/migrateLog.ts';
import { parseTokenBalances } from '../detector/laserstream.ts';
import { computePrice } from '../positions/pricing.ts';
import { deriveBondingCurvePda } from '../enrichment/curve.ts';
import type { PoolMark } from './economics.ts';

export interface MigrateTx {
  signature: string;
  slot: number | null;
  ok: boolean;
  /**
   * Landed = ok AND it moved token balances. A migrate on an already
   * migrated curve succeeds as a no-op ("Bonding curve already migrated",
   * observed live 2026-10-05: a second caller in the winner's slot), so
   * `ok` alone counts losers as winners. Excludes that log as well, since a
   * loser's bundled pool buy still moves balances.
   */
  landed: boolean;
  /** Fee payer (account key 0). */
  caller: string | null;
  /**
   * The graduating mint: the only non-WSOL mint, or — when the pool quotes in
   * another mint (Token-2022 curves graduating against e.g. SPCX) — the mint
   * whose bonding-curve PDA held it before the tx. Null when unresolvable.
   */
  mint: string | null;
  /** Ran a pump.fun Buy before Migrate in this tx (completion bundled). */
  curveBuyBefore: boolean;
  /** Ran a PumpSwap Buy after Migrate in this tx (first position bundled). */
  poolBuyAfter: boolean;
  /** Mint token program when supplied by transaction token-balance metadata. */
  tokenProgram?: string | null;
  pre: TxTokenBalance[];
  post: TxTokenBalance[];
}

interface ProgramInstruction {
  program: string;
  name: string;
}

const INVOKE = /^Program (\w+) invoke \[\d+\]$/;
const EXIT = /^Program (\w+) (?:success|failed)/;
const IX = /^Program log: Instruction: (\w+)\s*$/;
/**
 * pump.fun's no-op path. Balances alone are not enough: a loser that bundles
 * MigrateV2 with a PumpSwap buy still moves balances (live 2026-10-05,
 * one slot behind the winner).
 */
const ALREADY_MIGRATED = /already migrated/i;

/** Ordered (program, instruction name) pairs, attributed through the CPI stack. */
export function instructionTrace(logs: readonly string[]): ProgramInstruction[] {
  const stack: string[] = [];
  const out: ProgramInstruction[] = [];
  for (const line of logs) {
    const inv = INVOKE.exec(line);
    if (inv) {
      stack.push(inv[1]!);
      continue;
    }
    if (EXIT.test(line)) {
      stack.pop();
      continue;
    }
    const ix = IX.exec(line);
    if (ix && stack.length) out.push({ program: stack[stack.length - 1]!, name: ix[1]! });
  }
  return out;
}

/** A LaserStream transaction update → MigrateTx, or null when it is not a migrate attempt. */
export function extractMigrateTx(data: unknown): MigrateTx | null {
  const outer = (data as { transaction?: { transaction?: unknown; slot?: unknown } } | null)?.transaction;
  if (!outer) return null;
  const inner = (outer.transaction ?? outer) as {
    signature?: unknown;
    meta?: {
      err?: unknown;
      logMessages?: unknown;
      preTokenBalances?: unknown[];
      postTokenBalances?: unknown[];
    };
    transaction?: { message?: { accountKeys?: unknown[] } };
  };
  const meta = inner.meta;
  if (!meta) return null;
  const logs = Array.isArray(meta.logMessages) ? (meta.logMessages as string[]) : [];
  if (!logs.some((l) => MIGRATE_LOG.test(l))) return null;
  const signature = encode(inner.signature);
  if (!signature) return null;
  const rawBalances = [...(meta.preTokenBalances ?? []), ...(meta.postTokenBalances ?? [])] as Array<{ mint?: unknown; programId?: unknown }>;
  const pre = parseTokenBalances((meta.preTokenBalances ?? []) as never[]);
  const post = parseTokenBalances((meta.postTokenBalances ?? []) as never[]);
  const mints = [...new Set([...pre, ...post].map((b) => b.mint))].filter((m) => m !== WSOL_MINT);
  const trace = instructionTrace(logs);
  const migrateIdx = trace.findIndex((t) => t.program === PROGRAM_IDS.PUMP_FUN && /^Migrate(?:V\d+)?$/.test(t.name));
  const curveBuyBefore = migrateIdx > 0 && trace.slice(0, migrateIdx).some((t) => t.program === PROGRAM_IDS.PUMP_FUN && /^Buy/.test(t.name));
  const poolBuyAfter = migrateIdx >= 0 && trace.slice(migrateIdx + 1).some((t) => t.program === PROGRAM_IDS.PUMP_SWAP && /^Buy/.test(t.name));
  const key0 = inner.transaction?.message?.accountKeys?.[0];
  const slot = Number(outer.slot);
  const ok = meta.err === null || meta.err === undefined;
  const mint = mints.length === 1 ? mints[0]! : (mints.find((m) => pre.some((b) => b.mint === m && b.owner === deriveBondingCurvePda(m))) ?? null);
  const tokenProgram = rawBalances.find((b) => b.mint === mint && typeof b.programId === 'string')?.programId;
  return {
    signature,
    slot: Number.isFinite(slot) ? slot : null,
    ok,
    landed: ok && !logs.some((l) => ALREADY_MIGRATED.test(l)) && balancesMoved(pre, post),
    caller: key0 === undefined ? null : encode(key0),
    mint,
    curveBuyBefore,
    poolBuyAfter,
    tokenProgram: typeof tokenProgram === 'string' ? tokenProgram : null,
    pre,
    post,
  };
}

/** True when any (mint, owner) token balance differs between pre and post. */
export function balancesMoved(pre: readonly TxTokenBalance[], post: readonly TxTokenBalance[]): boolean {
  const key = (b: TxTokenBalance) => `${b.mint}:${b.owner ?? ''}`;
  const before = new Map<string, bigint>();
  for (const b of pre) before.set(key(b), (before.get(key(b)) ?? 0n) + b.amount);
  const after = new Map<string, bigint>();
  for (const b of post) after.set(key(b), (after.get(key(b)) ?? 0n) + b.amount);
  for (const k of new Set([...before.keys(), ...after.keys()])) if ((before.get(k) ?? 0n) !== (after.get(k) ?? 0n)) return true;
  return false;
}

/**
 * The pool a landed migrate created and the mint it quotes in: the owner
 * holding the most of `mint` after the tx (the base vault's authority), and
 * that owner's other balance. Canonical pools quote in WSOL; a non-WSOL
 * quote pool is real (live 2026-10-05) and is not SOL-priced.
 */
export function poolOf(tx: MigrateTx, mint: string): { poolOwner: string; quoteMint: string | null } | null {
  let best: TxTokenBalance | null = null;
  for (const b of tx.post) if (b.mint === mint && b.owner && (!best || b.amount > best.amount)) best = b;
  if (!best?.owner || best.amount <= 0n) return null;
  const quote = tx.post.find((b) => b.owner === best!.owner && b.mint !== mint);
  return { poolOwner: best.owner, quoteMint: quote?.mint ?? null };
}

export interface MigrateEntry {
  reach: PoolMark;
  open: PoolMark | null;
  /** Tokens the tx's non-pool owners gained (raw). */
  bundledTokens: bigint;
  poolBase: bigint;
  poolQuoteLamports: bigint;
}

/** The current paper-coverage gate uses observed post-transaction effective depth. */
export function isThinPool(reach: PoolMark | null, thresholdSol: number): boolean {
  return reach !== null && reach.quoteSol < thresholdSol;
}

function sumBy(bs: readonly TxTokenBalance[], pred: (b: TxTokenBalance) => boolean): bigint {
  let s = 0n;
  for (const b of bs) if (pred(b)) s += b.amount;
  return s;
}

/**
 * Price the new pool from a landed migrate tx's post token balances
 * (pool-owned vaults) and the pool's virtual quote reserve.
 */
export function migrateEntry(
  tx: MigrateTx,
  mint: string,
  poolKey: string,
  virtualQuoteLamports: bigint,
  decimals = 6,
): MigrateEntry | null {
  const poolBase = sumBy(tx.post, (b) => b.owner === poolKey && b.mint === mint);
  const poolQuoteLamports = sumBy(tx.post, (b) => b.owner === poolKey && b.mint === WSOL_MINT);
  if (poolBase <= 0n || poolQuoteLamports <= 0n) return null;
  const reachPrice = computePrice(poolBase, poolQuoteLamports, decimals, virtualQuoteLamports);
  if (!(reachPrice > 0)) return null;
  const reach: PoolMark = { price: reachPrice, quoteSol: Number(poolQuoteLamports + virtualQuoteLamports) / LAMPORTS_PER_SOL };

  // Per-owner token gains, pool vault excluded.
  const delta = new Map<string, bigint>();
  for (const b of tx.pre) if (b.mint === mint && b.owner && b.owner !== poolKey) delta.set(b.owner, (delta.get(b.owner) ?? 0n) - b.amount);
  for (const b of tx.post) if (b.mint === mint && b.owner && b.owner !== poolKey) delta.set(b.owner, (delta.get(b.owner) ?? 0n) + b.amount);
  let bundledTokens = 0n;
  for (const d of delta.values()) if (d > 0n) bundledTokens += d;

  let open: PoolMark | null;
  if (!tx.poolBuyAfter) open = reach;
  else if (tx.curveBuyBefore || bundledTokens <= 0n) open = null;
  else {
    const ratio = Number(poolBase) / Number(poolBase + bundledTokens);
    open = { price: reach.price * ratio * ratio, quoteSol: reach.quoteSol * ratio };
  }
  return { reach, open, bundledTokens, poolBase, poolQuoteLamports };
}

function encode(k: unknown): string | null {
  if (typeof k === 'string') return k;
  if (k instanceof Uint8Array) return base58Encode(k);
  if (Array.isArray(k)) return base58Encode(Uint8Array.from(k as number[]));
  return null;
}
