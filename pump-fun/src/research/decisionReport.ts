import type { DB } from '../persistence/db.ts';
import type { Config } from '../config/schema.ts';
import { buildDataset, candidateFeatureInput, type CandRow } from './dataset.ts';
import type { BarrierSpec, CostSpec } from './labels.ts';
import { brier, calibrationBins, ece, fitPlatt, logLoss } from './logreg.ts';
import { bootstrapCI, mean } from './stats.ts';
import { evaluateGate } from '../decision/policy.ts';
import type { EntryStateInput } from '../decision/entryState.ts';
import type { Answer } from '../decision/types.ts';

/**
 * Decision-model research (feat/jev-shadow, plan phase 4): replay inputs from
 * history, and a calibration / policy report joining decision_calls to the
 * same triple-barrier labels the learned filter trains on.
 */

// ---------------------------------------------------------------- replay ----

export interface ReplayInput {
  mint: string;
  input: EntryStateInput;
}

/**
 * Latest canonical (H12-pass) candidate per mint, rebuilt into the exact
 * EntryStateInput live would have sent. By default only rows whose tx stats
 * are clean (txFlowVersion >= 2); mints already scored by `provider` +
 * `modelVersion` are skipped so a replay can resume.
 */
export function loadReplayInputs(
  db: DB,
  opts: { provider: string; modelVersion: string; includeV1?: boolean; limit?: number },
): ReplayInput[] {
  const rows = db
    .prepare(
      `SELECT c.mint, c.created_at, c.early_flow_net_sol, c.early_flow_rate, c.pool_sol_at_entry, c.top10_share,
              c.max_holder_share, c.creator_share, c.rugcheck_score, c.has_socials, c.mint_age_ms, c.mcap_sol_at_entry,
              c.pool_move_pct, c.sellability_status, c.momentum_window_ms, c.features_json, c.population_ok,
              c.soft_score, c.hard_check_results
       FROM candidates c
       WHERE c.rowid = (SELECT MAX(rowid) FROM candidates WHERE mint = c.mint)
         AND c.population_ok = 1
         AND NOT EXISTS (SELECT 1 FROM decision_calls d
                         WHERE d.mint = c.mint AND d.phase = 'entry' AND d.ok = 1
                           AND d.provider = ? AND d.model_version = ?)
       ORDER BY c.rowid ASC`,
    )
    .all(opts.provider, opts.modelVersion) as unknown as Array<
    CandRow & { mint: string; soft_score: number | null; hard_check_results: string | null }
  >;
  const out: ReplayInput[] = [];
  for (const r of rows) {
    if (!opts.includeV1 && !isTxFlowV2(r.features_json)) continue;
    out.push({
      mint: r.mint,
      input: { ...candidateFeatureInput(r), softScore: r.soft_score, checks: checksFrom(r.hard_check_results) },
    });
    if (opts.limit !== undefined && out.length >= opts.limit) break;
  }
  return out;
}

function isTxFlowV2(featuresJson: string | null): boolean {
  if (!featuresJson) return false;
  try {
    const v = JSON.parse(featuresJson) as { earlyFlow?: { txFlowVersion?: number } };
    return (v.earlyFlow?.txFlowVersion ?? 1) >= 2;
  } catch {
    return false;
  }
}

/** Same shape the live pipeline sends: hard-check id -> status. */
function checksFrom(json: string | null): Record<string, string> | null {
  if (!json) return null;
  try {
    const arr = JSON.parse(json) as Array<{ id?: string; status?: string }>;
    const entries = arr.filter((c) => c.id && c.status).map((c) => [c.id!, c.status!] as const);
    return entries.length ? Object.fromEntries(entries) : null;
  } catch {
    return null;
  }
}

// ---------------------------------------------------------------- report ----

export interface DecisionRow {
  mint: string;
  mode: string;
  provider: string;
  modelVersion: string;
  questionSetVersion: number;
  answers: Answer[];
  decisionProb: number | null;
  modelProb: number | null;
}

/**
 * One scored call per mint: live (shadow / gate) beats replay, then newest.
 * Live is what the bot actually saw; replay rebuilds from stored features.
 */
export function loadDecisionRows(db: DB, opts: { provider?: string } = {}): DecisionRow[] {
  const rows = db
    .prepare(
      `SELECT d.mint, d.mode, d.provider, d.model_version, d.question_set_version, d.answers_json, d.decision_prob,
              (SELECT c.model_prob FROM candidates c WHERE c.mint = d.mint ORDER BY c.rowid DESC LIMIT 1) AS model_prob
       FROM decision_calls d
       WHERE d.phase = 'entry' AND d.ok = 1 AND d.answers_json IS NOT NULL ${opts.provider ? 'AND d.provider = ?' : ''}
       ORDER BY CASE d.mode WHEN 'replay' THEN 1 ELSE 0 END, d.id DESC`,
    )
    .all(...(opts.provider ? [opts.provider] : [])) as Array<{
    mint: string;
    mode: string;
    provider: string;
    model_version: string | null;
    question_set_version: number;
    answers_json: string;
    decision_prob: number | null;
    model_prob: number | null;
  }>;
  const seen = new Set<string>();
  const out: DecisionRow[] = [];
  for (const r of rows) {
    if (seen.has(r.mint)) continue;
    seen.add(r.mint);
    let answers: Answer[] = [];
    try {
      answers = JSON.parse(r.answers_json) as Answer[];
    } catch {
      continue;
    }
    out.push({
      mint: r.mint,
      mode: r.mode,
      provider: r.provider,
      modelVersion: r.model_version ?? '?',
      questionSetVersion: r.question_set_version,
      answers,
      decisionProb: r.decision_prob,
      modelProb: r.model_prob,
    });
  }
  return out;
}

export interface MintLabel {
  label: 0 | 1;
  netReturn: number;
  arm: string;
}

/**
 * Triple-barrier label per mint, from the first available arm in `arms`
 * preference order (default: the graduation-baseline shadow path, then the
 * earliest confirm arm). Same labeller and costs as research:train.
 */
export function labelsByMint(
  db: DB,
  opts: { barrier: BarrierSpec; cost: CostSpec; arms: readonly string[] },
): Map<string, MintLabel> {
  const samples = buildDataset(db, { barrier: opts.barrier, cost: opts.cost, arms: opts.arms, canonicalOnly: true });
  const out = new Map<string, MintLabel>();
  const rank = (arm: string) => opts.arms.indexOf(arm);
  for (const s of samples) {
    const prev = out.get(s.mint);
    if (prev && rank(prev.arm) <= rank(s.arm)) continue;
    out.set(s.mint, { label: s.label, netReturn: s.netReturn, arm: s.arm });
  }
  return out;
}

export interface QuestionCalibration {
  id: string;
  n: number;
  baseRate: number;
  brier: number;
  logLoss: number;
  ece: number;
  bins: ReturnType<typeof calibrationBins>;
  platt: { a: number; b: number } | null;
}

export interface PolicyEval {
  n: number;
  taken: number;
  allMean: { point: number; lo: number; hi: number };
  takenMean: { point: number; lo: number; hi: number };
  /** Paired: mean(net × taken) − mean(net) over the same candidates — the gate's lift per candidate. */
  lift: { point: number; lo: number; hi: number };
}

export interface DecisionReport {
  groups: Array<{
    key: string;
    provider: string;
    modelVersion: string;
    questionSetVersion: number;
    n: number;
    /** Only `continuation` is labelled directly; the others are proxies and reported for reference. */
    continuation: QuestionCalibration | null;
    toxicFlowVsLoss: QuestionCalibration | null;
    rugRiskVsDeepLoss: QuestionCalibration | null;
    learnedFilter: QuestionCalibration | null;
    policy: PolicyEval | null;
  }>;
  unlabelled: number;
}

function calib(id: string, p: number[], y: number[], withPlatt: boolean): QuestionCalibration | null {
  if (p.length === 0) return null;
  return {
    id,
    n: p.length,
    baseRate: mean(y),
    brier: brier(p, y),
    logLoss: logLoss(p, y),
    ece: ece(p, y),
    bins: calibrationBins(p, y),
    platt: withPlatt && p.length >= 30 ? fitPlatt(p, y) : null,
  };
}

const probOf = (answers: readonly Answer[], id: string): number | null => {
  const a = answers.find((x) => x.id === id);
  return a && typeof a.value === 'number' && Number.isFinite(a.value) ? a.value : null;
};

/**
 * Per (provider, model version, question set): calibration of each
 * probability answer against realized outcomes, the learned filter on the SAME
 * rows as a baseline, and the configured gate's policy lift with bootstrap CIs.
 */
export function buildDecisionReport(
  rows: readonly DecisionRow[],
  labels: ReadonlyMap<string, MintLabel>,
  opts: { gate: Config['decision']['gate']; seed: number; deepLossPct?: number },
): DecisionReport {
  const deepLoss = -(opts.deepLossPct ?? 50) / 100;
  const groups = new Map<string, DecisionRow[]>();
  let unlabelled = 0;
  for (const r of rows) {
    if (!labels.has(r.mint)) {
      unlabelled++;
      continue;
    }
    const key = `${r.provider} · ${r.modelVersion} · q${r.questionSetVersion}`;
    (groups.get(key) ?? groups.set(key, []).get(key)!).push(r);
  }
  const out: DecisionReport['groups'] = [];
  for (const [key, g] of groups) {
    const pairs = (id: string, outcome: (l: MintLabel) => 0 | 1) => {
      const p: number[] = [];
      const y: number[] = [];
      for (const r of g) {
        const v = probOf(r.answers, id);
        if (v === null) continue;
        p.push(v);
        y.push(outcome(labels.get(r.mint)!));
      }
      return { p, y };
    };
    const cont = pairs('continuation', (l) => l.label);
    const toxic = pairs('toxic_flow', (l) => (l.netReturn < 0 ? 1 : 0));
    const rug = pairs('rug_risk', (l) => (l.netReturn <= deepLoss ? 1 : 0));
    const lf = { p: [] as number[], y: [] as number[] };
    for (const r of g) {
      if (r.modelProb === null) continue;
      lf.p.push(r.modelProb);
      lf.y.push(labels.get(r.mint)!.label);
    }

    const net: number[] = [];
    const gated: number[] = [];
    for (const r of g) {
      const l = labels.get(r.mint)!;
      const pass = evaluateGate({ answers: r.answers }, opts.gate).veto === null;
      net.push(l.netReturn);
      gated.push(pass ? l.netReturn : 0);
    }
    const takenNets = g.filter((r) => evaluateGate({ answers: r.answers }, opts.gate).veto === null).map((r) => labels.get(r.mint)!.netReturn);
    const idx = net.map((_, i) => i);
    const policy: PolicyEval | null = g.length
      ? {
          n: g.length,
          taken: takenNets.length,
          allMean: bootstrapCI(net, { seed: opts.seed }),
          takenMean: bootstrapCI(takenNets, { seed: opts.seed }),
          lift: bootstrapCI(idx, { seed: opts.seed, stat: (s) => mean(s.map((i) => gated[i]! - net[i]!)) }),
        }
      : null;

    const first = g[0]!;
    out.push({
      key,
      provider: first.provider,
      modelVersion: first.modelVersion,
      questionSetVersion: first.questionSetVersion,
      n: g.length,
      continuation: calib('continuation', cont.p, cont.y, true),
      toxicFlowVsLoss: calib('toxic_flow', toxic.p, toxic.y, false),
      rugRiskVsDeepLoss: calib('rug_risk', rug.p, rug.y, false),
      learnedFilter: calib('model_prob', lf.p, lf.y, false),
      policy,
    });
  }
  return { groups: out.sort((a, b) => b.n - a.n), unlabelled };
}

const f3 = (x: number) => (Number.isFinite(x) ? x.toFixed(3) : 'n/a');
const pct = (x: number) => (Number.isFinite(x) ? `${(x * 100).toFixed(2)}%` : 'n/a');
const ci = (c: { point: number; lo: number; hi: number }) => `${pct(c.point)} [${pct(c.lo)}, ${pct(c.hi)}]`;

export function renderDecisionReport(r: DecisionReport, ctx: { db: string; barrier: BarrierSpec; arms: readonly string[] }): string {
  const lines: string[] = [
    '# Decision model — calibration & policy report',
    '',
    `DB \`${ctx.db}\` · labels: triple barrier +${ctx.barrier.tpPct}% / −${ctx.barrier.slPct}% / ${ctx.barrier.timeStopMs / 1000}s after costs, arms ${ctx.arms.join(' > ')} · ${r.unlabelled} scored mints without a label path`,
    '',
    '> Replay states are rebuilt from stored features and exclude anything not persisted at the time. Live shadow rows take precedence per mint. `stub-v1` is a no-edge stand-in: its numbers are plumbing, not signal.',
    '',
  ];
  if (r.groups.length === 0) lines.push('_No labelled decision calls yet._', '');
  for (const g of r.groups) {
    lines.push(`## ${g.key} — n ${g.n}`, '');
    lines.push('| question | outcome | n | base rate | Brier | log loss | ECE |', '|---|---|---|---|---|---|---|');
    const row = (c: QuestionCalibration | null, outcome: string) =>
      c && lines.push(`| ${c.id} | ${outcome} | ${c.n} | ${pct(c.baseRate)} | ${f3(c.brier)} | ${f3(c.logLoss)} | ${f3(c.ece)} |`);
    row(g.continuation, 'profitable after costs');
    row(g.learnedFilter, 'profitable after costs (baseline, same rows)');
    row(g.toxicFlowVsLoss, 'net < 0 (proxy)');
    row(g.rugRiskVsDeepLoss, 'net ≤ −50% (proxy)');
    lines.push('');
    if (g.continuation) {
      lines.push('Reliability — continuation', '', '| bin | n | mean p | observed |', '|---|---|---|---|');
      for (const b of g.continuation.bins) if (b.n) lines.push(`| ${b.lo.toFixed(1)}–${b.hi.toFixed(1)} | ${b.n} | ${f3(b.meanP)} | ${f3(b.freq)} |`);
      lines.push('');
      if (g.continuation.platt) {
        const { a, b } = g.continuation.platt;
        lines.push(`Platt fit (paste into \`decision.gate.calibration.continuation\` only after review): \`{ a: ${a.toFixed(4)}, b: ${b.toFixed(4)} }\``, '');
      }
    }
    if (g.policy) {
      const p = g.policy;
      lines.push(
        `Policy (configured gate): takes ${p.taken}/${p.n} · mean net all ${ci(p.allMean)} · taken ${ci(p.takenMean)} · lift per candidate ${ci(p.lift)}`,
        '',
        p.lift.lo > 0 ? '**Gate lift CI lower bound > 0.**' : 'Gate lift CI includes 0 — not evidence for gating yet.',
        '',
      );
    }
  }
  return lines.join('\n') + '\n';
}
