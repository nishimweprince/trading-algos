/**
 * Offline screening replay (2026-09-28 latency work). Re-runs the guardrail
 * engine at HEAD over stored candidates rows — once as stored and once as
 * `population.earlyVeto` screening would have seen them (H4 probe + features
 * skipped) — and classifies every difference against the stored verdict.
 *
 * Pure: the CLI (screen-replay-cli.ts) owns the DB and printing.
 */
import type { Config } from '../config/schema.ts';
import type { CandidateVerdict, CheckResult, GraduationEvent } from '../core/types.ts';
import type { Candidate, EnrichmentData } from '../enrichment/types.ts';
import type { Repositories } from '../persistence/repositories.ts';
import { GuardrailEngine } from '../guardrails/engine.ts';
import { markEarlyVeto } from '../guardrails/checks/population.ts';
import { LAMPORTS_PER_SOL } from '../core/constants.ts';

/** One stored candidates row joined to its graduation. */
export interface ReplayRow {
  mint: string;
  enrichmentJson: string | null;
  hardCheckResults: string | null;
  verdict: 'accept' | 'veto';
  primaryVetoCode: string | null;
  slot: number | null;
  venue: string | null;
  feedSource: string | null;
  poolAddress: string | null;
  /** graduations.created_at as epoch ms (second resolution): stands in for detectedAtMs. */
  detectedAtMs: number | null;
}

export type DiffClass =
  | 'check_added_since' // the stored row predates a check HEAD runs (H11 / H12 / H13 rollout)
  | 'rule_changed_since' // a known H12 rule change landed after the row was recorded (see classify)
  | 'db_state' // H8 / H10 read tables and files that move over time
  | 'wallet_sol' // H7 impact uses the live wallet; replay has none
  | 'launch_clock_approx' // H12 aged from graduations.created_at (1 s) instead of detection wall-clock
  | 'score' // soft score moved across minEntryScore
  | 'unexplained';

export interface ReplayResult {
  mint: string;
  stored: { verdict: string; primary: string | null };
  replay: { verdict: string; primary: string | null };
  early: { verdict: string; primary: string | null; marked: boolean };
  /** Checks whose status differs between the stored row and the HEAD replay. */
  changedChecks: Array<{ id: string; stored: string | null; replay: string; detail?: string }>;
  /** Replay vs stored verdict / primary code: why they differ (empty = identical). */
  diff: DiffClass[];
  /** Early-veto vs full replay primary code differs because H4 / H13 would have been primary. */
  earlyPrimaryShift: boolean;
  /** Stored pool quote reserve < 1 SOL: live screening now re-reads it, the verdict may change. */
  poolRereadCandidate: boolean;
  /** RugCheck missing in the stored row: live, a late answer is now unknown (was waited on). */
  rugcheckUnknown: boolean;
}

/** Fields persisted as decimal strings (safeJson) that the engine reads as bigint. */
const BIGINT_PATHS: ReadonlyArray<readonly string[]> = [
  ['mintInfo', 'supply'],
  ['pool', 'baseReserve'],
  ['pool', 'quoteReserveLamports'],
  ['pool', 'lpMintSupply'],
  ['holders', 'supply'],
  ['earlyFlow', 'quoteReserveStartLamports'],
  ['earlyFlow', 'quoteReserveEndLamports'],
];

/** Parse candidates.enrichment_json back into EnrichmentData (bigints restored). */
export function reviveEnrichment(json: string): EnrichmentData {
  const e = JSON.parse(json) as Record<string, any>; // eslint-disable-line @typescript-eslint/no-explicit-any
  for (const [a, b] of BIGINT_PATHS) {
    const v = e[a!]?.[b!];
    if (typeof v === 'string' || typeof v === 'number') e[a!][b!] = BigInt(v);
  }
  if (Array.isArray(e.holders?.holders)) {
    for (const h of e.holders.holders) if (typeof h.amount === 'string' || typeof h.amount === 'number') h.amount = BigInt(h.amount);
  }
  e.unknowns ??= [];
  e.elapsedMs ??= 0;
  return e as EnrichmentData;
}

export function candidateFromRow(r: ReplayRow): Candidate | null {
  if (!r.enrichmentJson) return null;
  const graduation: GraduationEvent = {
    mint: r.mint,
    venue: r.venue === 'raydium' ? 'raydium' : 'pumpswap',
    poolAddress: r.poolAddress ?? '',
    slot: r.slot ?? 0,
    feedSource: (r.feedSource ?? 'pumpportal') as GraduationEvent['feedSource'],
    receivedAtNs: 0n,
    ...(r.detectedAtMs !== null ? { detectedAtMs: r.detectedAtMs } : {}),
  };
  return { graduation, enrichment: reviveEnrichment(r.enrichmentJson) };
}

/** The candidate early-veto screening would have evaluated: probe + features never ran. */
export function earlyVetoView(c: Candidate, config: Config): { candidate: Candidate; marked: boolean } {
  const e = structuredClone(c);
  const marked = markEarlyVeto(e, config) !== null;
  if (marked) {
    delete e.enrichment.sellable;
    delete e.enrichment.features;
  }
  return { candidate: e, marked };
}

export function replayRow(r: ReplayRow, config: Config, repos: Repositories): ReplayResult | null {
  const c = candidateFromRow(r);
  if (!c) return null;
  const engine = new GuardrailEngine(config, repos);
  const replay = engine.evaluate(structuredClone(c));
  const ev = earlyVetoView(c, config);
  const early = engine.evaluate(ev.candidate);
  const storedChecks = parseChecks(r.hardCheckResults);
  const changedChecks = replay.hardChecks
    .filter((h) => storedChecks.get(h.id)?.status !== h.status)
    .map((h) => ({ id: h.id, stored: storedChecks.get(h.id)?.status ?? null, replay: h.status, ...(h.detail ? { detail: h.detail } : {}) }));
  const primary = (v: CandidateVerdict) => v.vetoReasons[0] ?? null;
  const same = replay.verdict === r.verdict && primary(replay) === r.primaryVetoCode;
  const pool = c.enrichment.pool;
  return {
    mint: r.mint,
    stored: { verdict: r.verdict, primary: r.primaryVetoCode },
    replay: { verdict: replay.verdict, primary: primary(replay) },
    early: { verdict: early.verdict, primary: primary(early), marked: ev.marked },
    changedChecks,
    diff: same ? [] : classify(changedChecks, storedChecks, replay, r),
    earlyPrimaryShift: primary(early) !== primary(replay) && ['H4', 'H13'].includes(primary(replay) ?? ''),
    poolRereadCandidate: pool !== undefined && Number(pool.quoteReserveLamports) < LAMPORTS_PER_SOL,
    rugcheckUnknown: c.enrichment.rugcheckScore === undefined,
  };
}

function parseChecks(json: string | null): Map<string, CheckResult> {
  if (!json) return new Map();
  try {
    return new Map((JSON.parse(json) as CheckResult[]).map((c) => [c.id, c]));
  } catch {
    return new Map();
  }
}

function classify(
  changed: ReplayResult['changedChecks'],
  stored: Map<string, CheckResult>,
  replay: CandidateVerdict,
  r: ReplayRow,
): DiffClass[] {
  const out = new Set<DiffClass>();
  for (const c of changed) {
    if (c.stored === null) out.add('check_added_since');
    else if (c.id === 'H8' || c.id === 'H10') out.add('db_state');
    else if (c.id === 'H7' && /impact/.test(`${c.detail ?? ''} ${stored.get('H7')?.detail ?? ''}`)) out.add('wallet_sol');
    else if (c.id === 'H12' && h12RuleChange(stored.get('H12'), replay.hardChecks.find((h) => h.id === 'H12'))) out.add('rule_changed_since');
    else if (c.id === 'H12' && /launch_clock/.test(`${c.detail ?? ''} ${stored.get('H12')?.detail ?? ''}`)) out.add('launch_clock_approx');
    else out.add('unexplained');
  }
  if (changed.length === 0) {
    // Same check statuses: the verdict can still move on LOW_SCORE / relaxed-risk tails.
    const tail = (code: string | null) => code === 'LOW_SCORE' || code === 'RELAXED_DISABLED' || code === 'MULTI_RELAXED_RISK' || code === null;
    out.add(tail(r.primaryVetoCode) && tail(replay.vetoReasons[0] ?? null) ? 'score' : 'unexplained');
  }
  return [...out];
}

/**
 * H12 rule changes made on 2026-09-28 that rows recorded earlier that day predate:
 *  - ec2e271: a missing pool fails closed (`no_pool` was `unknown`, which dry-run never vetoes);
 *  - 9e3eb64: the curve scan's oldest slot is a mint-age lower bound (`curve_lower_bound`),
 *    so a `mint_age_unknown` fail on a long curve history now passes.
 */
function h12RuleChange(stored: CheckResult | undefined, replay: CheckResult | undefined): boolean {
  if (!stored || !replay) return false;
  if (stored.status === 'unknown' && stored.reason === 'no_pool' && replay.status === 'fail' && replay.reason === 'no_pool') return true;
  return stored.reason === 'mint_age_unknown' && replay.status === 'pass' && /curve_lower_bound/.test(replay.detail ?? '');
}

/** Markdown report over all replayed rows. */
export function renderReplay(results: ReplayResult[], skipped: number): string {
  const n = results.length;
  const diffs = results.filter((r) => r.diff.length > 0);
  const byClass = new Map<string, number>();
  for (const r of diffs) for (const d of r.diff) byClass.set(d, (byClass.get(d) ?? 0) + 1);
  const earlyVerdictChanges = results.filter((r) => r.early.verdict !== r.replay.verdict);
  const shifts = results.filter((r) => r.earlyPrimaryShift);
  const otherEarlyPrimary = results.filter((r) => r.early.primary !== r.replay.primary && !r.earlyPrimaryShift);
  const short = (m: string) => `${m.slice(0, 4)}…${m.slice(-4)}`;
  const lines: string[] = [];
  lines.push(`# Screening replay (HEAD config)`, '');
  lines.push(`- rows replayed: **${n}** (skipped, no enrichment_json: ${skipped})`);
  lines.push(`- replay vs stored verdict/primary identical: **${n - diffs.length}**; different: **${diffs.length}**`);
  for (const [k, v] of [...byClass.entries()].sort((a, b) => b[1] - a[1])) lines.push(`  - ${k}: ${v}`);
  lines.push(`- early-veto marked: **${results.filter((r) => r.early.marked).length}**`);
  lines.push(`- early-veto verdict ≠ full replay verdict: **${earlyVerdictChanges.length}**`);
  lines.push(`- early-veto primary moved off H4/H13 to the next failing check (accepted trade-off): **${shifts.length}**`);
  lines.push(`- early-veto primary changed for any other reason: **${otherEarlyPrimary.length}**`);
  lines.push(`- stored pool < 1 SOL (live now re-reads; verdict may change): **${results.filter((r) => r.poolRereadCandidate).length}**`);
  lines.push(`- stored rugcheck missing (live: unknown unless it arrives before the verdict): **${results.filter((r) => r.rugcheckUnknown).length}**`);
  lines.push('');
  if (diffs.length) {
    lines.push('## Replay vs stored differences', '');
    lines.push('| mint | stored | replay | class | changed checks |', '|---|---|---|---|---|');
    for (const r of diffs) {
      const checks = r.changedChecks.map((c) => `${c.id} ${c.stored ?? '∅'}→${c.replay}`).join(', ') || '—';
      lines.push(`| ${short(r.mint)} | ${r.stored.verdict} ${r.stored.primary ?? ''} | ${r.replay.verdict} ${r.replay.primary ?? ''} | ${r.diff.join(', ')} | ${checks} |`);
    }
    lines.push('');
  }
  if (shifts.length || earlyVerdictChanges.length || otherEarlyPrimary.length) {
    lines.push('## Early-veto vs full replay', '');
    lines.push('| mint | full | early | note |', '|---|---|---|---|');
    for (const r of [...earlyVerdictChanges, ...shifts, ...otherEarlyPrimary]) {
      const note = r.early.verdict !== r.replay.verdict ? 'VERDICT CHANGED' : r.earlyPrimaryShift ? 'H4/H13 primary → next failing check' : 'primary changed';
      lines.push(`| ${short(r.mint)} | ${r.replay.verdict} ${r.replay.primary ?? ''} | ${r.early.verdict} ${r.early.primary ?? ''} | ${note} |`);
    }
    lines.push('');
  }
  return lines.join('\n');
}
