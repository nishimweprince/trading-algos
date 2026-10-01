/**
 * Offline screening replay. Re-runs the guardrail engine at HEAD (the fast
 * path, 2026-09-28) over stored candidates rows and reports what it would
 * have decided differently from the stored verdict — with, for every newly
 * accepted row, the shadow tracker's paper outcome of that (vetoed) mint.
 *
 * Replay approximations, stated in the report:
 *  - H6: the fast path reads the creator's ATA; here the creator's share
 *    comes from the stored holder snapshot (when one exists);
 *  - H12: the launch-stream coverage window did not exist when rows were
 *    recorded, so an unseen mint age still fails;
 *  - H8 / H10 / H13 read the DB and files as they are NOW.
 *
 * Pure: the CLI (screen-replay-cli.ts) owns the DB and printing.
 */
import type { Config } from '../config/schema.ts';
import type { CandidateVerdict, CheckResult, GraduationEvent } from '../core/types.ts';
import type { Candidate, EnrichmentData } from '../enrichment/types.ts';
import type { Repositories } from '../persistence/repositories.ts';
import { GuardrailEngine } from '../guardrails/engine.ts';

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

/** The shadow tracker's paper outcome for a mint (vetoed rows only). */
export interface ShadowOutcome {
  pnlPct: number | null;
  exitReason: string | null;
}

export interface ReplayResult {
  mint: string;
  stored: { verdict: string; primary: string | null; reasons: string[] };
  replay: { verdict: string; primary: string | null; reasons: string[] };
  /** Fast-path checks that failed, with their detail. */
  replayFails: Array<{ id: string; reason?: string; detail?: string }>;
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
  const enrichment = reviveEnrichment(r.enrichmentJson);
  // Pre-fast-path rows: derive what the fast read would have produced.
  const pool = enrichment.pool;
  if (!enrichment.creatorHolding && pool && enrichment.holders) {
    const share = enrichment.holders.holders.filter((h) => h.owner === pool.coinCreator).reduce((s, h) => s + h.share, 0);
    enrichment.creatorHolding = { creator: pool.coinCreator, share };
  }
  return { graduation, enrichment };
}

export function replayRow(r: ReplayRow, config: Config, repos: Repositories): ReplayResult | null {
  const c = candidateFromRow(r);
  if (!c) return null;
  const replay = new GuardrailEngine(config, repos).evaluate(c);
  const primary = (v: CandidateVerdict) => v.vetoReasons[0] ?? null;
  return {
    mint: r.mint,
    stored: { verdict: r.verdict, primary: r.primaryVetoCode, reasons: storedReasons(r.hardCheckResults, r.primaryVetoCode) },
    replay: { verdict: replay.verdict, primary: primary(replay), reasons: replay.vetoReasons },
    replayFails: replay.hardChecks
      .filter((h: CheckResult) => h.status === 'fail')
      .map((h) => ({ id: h.id, ...(h.reason ? { reason: h.reason } : {}), ...(h.detail ? { detail: h.detail } : {}) })),
  };
}

function storedReasons(json: string | null, primary: string | null): string[] {
  if (!json) return primary ? [primary] : [];
  try {
    const fails = (JSON.parse(json) as CheckResult[]).filter((c) => c.status === 'fail').map((c) => c.id);
    return fails.length ? fails : primary ? [primary] : [];
  } catch {
    return primary ? [primary] : [];
  }
}

/** Markdown report over all replayed rows. */
export function renderReplay(results: ReplayResult[], skipped: number, shadow: ReadonlyMap<string, ShadowOutcome> = new Map()): string {
  const n = results.length;
  const count = (f: (r: ReplayResult) => boolean) => results.filter(f).length;
  const flippedIn = results.filter((r) => r.stored.verdict === 'veto' && r.replay.verdict === 'accept');
  const flippedOut = results.filter((r) => r.stored.verdict === 'accept' && r.replay.verdict === 'veto');
  const failCounts = new Map<string, number>();
  for (const r of results) {
    for (const f of r.replayFails) {
      const k = f.reason ? `${f.id} ${f.reason}` : f.id;
      failCounts.set(k, (failCounts.get(k) ?? 0) + 1);
    }
  }
  const short = (m: string) => `${m.slice(0, 4)}…${m.slice(-4)}`;
  const lines: string[] = [];
  lines.push('# Screening replay (fast-path engine at HEAD)', '');
  lines.push(`- rows replayed: **${n}** (skipped, no enrichment_json: ${skipped})`);
  lines.push(`- stored accepts: **${count((r) => r.stored.verdict === 'accept')}** → replay accepts: **${count((r) => r.replay.verdict === 'accept')}**`);
  lines.push(`- veto → accept: **${flippedIn.length}**; accept → veto: **${flippedOut.length}**`);
  lines.push('- approximations: H6 from the stored holder snapshot; H12 without the launch-stream coverage window; H8/H10/H13 read the DB as it is now.');
  lines.push('');
  lines.push('## Replay failures by check', '', '| check | rows |', '|---|---|');
  for (const [k, v] of [...failCounts.entries()].sort((a, b) => b[1] - a[1])) lines.push(`| ${k} | ${v} |`);
  lines.push('');
  if (flippedIn.length) {
    const withOutcome = flippedIn.map((r) => ({ r, o: shadow.get(r.mint) })).filter((x) => x.o?.pnlPct != null);
    const pnls = withOutcome.map((x) => x.o!.pnlPct!);
    const mean = pnls.length ? pnls.reduce((a, b) => a + b, 0) / pnls.length : null;
    lines.push('## Veto → accept (with the shadow paper outcome of the vetoed mint)', '');
    lines.push(
      `- shadow outcomes: **${pnls.length}/${flippedIn.length}**` +
        (mean !== null ? `, mean ${mean.toFixed(1)}%, wins ${pnls.filter((p) => p > 0).length}/${pnls.length}` : ''),
    );
    lines.push('', '| mint | stored vetoes | shadow pnl % | exit |', '|---|---|---|---|');
    for (const r of flippedIn) {
      const o = shadow.get(r.mint);
      lines.push(`| ${short(r.mint)} | ${r.stored.reasons.join(', ')} | ${o?.pnlPct != null ? o.pnlPct.toFixed(1) : '—'} | ${o?.exitReason ?? '—'} |`);
    }
    lines.push('');
  }
  if (flippedOut.length) {
    lines.push('## Accept → veto', '', '| mint | replay fails |', '|---|---|');
    for (const r of flippedOut) lines.push(`| ${short(r.mint)} | ${r.replayFails.map((f) => `${f.id} ${f.detail ?? ''}`).join('; ')} |`);
    lines.push('');
  }
  return lines.join('\n');
}
