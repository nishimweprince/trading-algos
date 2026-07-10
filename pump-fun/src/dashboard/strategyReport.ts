/**
 * Strategy Week Review package for coding-model handoff (live pilot optimized).
 * Produces JSON + Markdown + CSV builders from SQLite.
 */
import type { DB } from '../persistence/db.ts';
import type { Config } from '../config/schema.ts';
import {
  computePerformanceStats,
  rangeToModifier,
  type PerformanceStats,
} from './analytics.ts';
import { getFunnelAnalytics, getVetoReasonBreakdown, listCheckFailRates } from './queries.ts';

export const STRATEGY_REPORT_SCHEMA_VERSION = 1;

export interface StrategyTradeRow {
  mint: string;
  netPnlSol: number;
  pnlPct: number | null;
  feesSol: number;
  exitReason: string;
  entrySoftScore: number | null;
  highVolatility: boolean | null;
  mfePct: number | null;
  maePct: number | null;
  leftOnTablePct: number | null;
  holdMs: number | null;
  timeToMfeMs: number | null;
  timeToMaeMs: number | null;
  detectToOpenMs: number | null;
  exitConfirmMs: number | null;
  momentumWindowMs: number | null;
  earlyFlowNetSol: number | null;
  earlyFlowRate: number | null;
  poolSolAtEntry: number | null;
  sizeMultiplier: number | null;
  mode: string | null;
  configHash: string | null;
  openedAt: string | null;
  closedAt: string | null;
  entryTx: string | null;
  exitTx: string | null;
  pathMarksJson: string | null;
  scoreComponentsJson: string | null;
}

export interface StratumRow {
  key: string;
  n: number;
  winRatePct: number;
  expectancySol: number;
  profitFactor: number;
  avgMfePct: number;
  avgMaePct: number;
  avgLeftOnTablePct: number;
  avgHoldMs: number;
  realizedPnlSol: number;
  underpowered: boolean;
}

export interface StrategyHypothesis {
  id: string;
  claim: string;
  evidence: Record<string, number | string | boolean>;
  suggestedTouchpoints: string[];
  suggestedChange: string;
  underpowered: boolean;
}

export interface StrategyWeekReport {
  schemaVersion: number;
  generatedAt: string;
  range: { label: string; modeFilter: string | null };
  configSessions: Array<{
    id: number;
    startedAt: string;
    endedAt: string | null;
    mode: string;
    configHash: string;
    gitCommit: string | null;
    knobs: unknown;
  }>;
  headline: PerformanceStats & {
    leftOnTableAvgPct: number;
    avgDetectToOpenMs: number;
    avgExitConfirmMs: number;
    emergencyExitCount: number;
    failedCount: number;
  };
  execution: {
    avgDetectToOpenMs: number;
    avgExitConfirmMs: number;
    totalFeesSol: number;
    failedCount: number;
    note: string;
  };
  strata: {
    byExitReason: StratumRow[];
    byScoreBucket: StratumRow[];
    byMomentumWindowMs: StratumRow[];
    byHighVolatility: StratumRow[];
    byHourUtc: StratumRow[];
  };
  exitQuality: {
    avgMfePct: number;
    avgMaePct: number;
    avgLeftOnTablePct: number;
    tp1ThenGiveback: { n: number; avgGivebackPct: number };
  };
  selection: {
    funnel: ReturnType<typeof getFunnelAnalytics>;
    checkFailRates: ReturnType<typeof listCheckFailRates>;
    vetoReasons: ReturnType<typeof getVetoReasonBreakdown>;
  };
  examples: {
    bestTrades: StrategyTradeRow[];
    worstTrades: StrategyTradeRow[];
    emergencyExits: StrategyTradeRow[];
  };
  hypotheses: StrategyHypothesis[];
  codeMap: Record<string, string>;
  caveats: string[];
}

const CODE_MAP = {
  scoring: 'src/guardrails/scoring.ts',
  exits: 'src/exits/engine.ts',
  entry: 'config.yaml#entry',
  guardrails: 'config.yaml#guardrails',
  risk: 'src/risk/manager.ts',
  positions: 'src/positions/manager.ts',
  fees: 'config.yaml#fees',
};

/** Live pilot: global claims need n≥10; strata need n≥5. */
const GLOBAL_N = 10;
const STRATA_N = 5;

export function buildStrategyWeekReport(
  db: DB,
  config: Config,
  opts: { range?: '24h' | '7d' | '30d' | 'all'; mode?: string | null; allowMixedModes?: boolean } = {},
): StrategyWeekReport {
  const range = opts.range ?? '7d';
  const modeFilter = opts.mode ?? config.mode;
  const allowMixed = opts.allowMixedModes === true;
  const trades = loadTrades(db, range, allowMixed ? null : modeFilter);
  const pnls = trades.map((t) => t.netPnlSol);
  const fees = trades.map((t) => t.feesSol);
  const stats = computePerformanceStats(pnls, fees);

  const lefts = trades.map((t) => t.leftOnTablePct).filter((x): x is number => x !== null);
  const detectMs = trades.map((t) => t.detectToOpenMs).filter((x): x is number => x !== null);
  const exitMs = trades.map((t) => t.exitConfirmMs).filter((x): x is number => x !== null);
  const mfe = trades.map((t) => t.mfePct).filter((x): x is number => x !== null);
  const mae = trades.map((t) => t.maePct).filter((x): x is number => x !== null);

  const emergencyExitCount = trades.filter((t) => t.exitReason === 'EMERGENCY_EXIT').length;
  const failedCount = countFailed(db, range, allowMixed ? null : modeFilter);

  const strata = {
    byExitReason: stratumBy(trades, (t) => t.exitReason || 'UNKNOWN'),
    byScoreBucket: stratumBy(trades, (t) => scoreBucket(t.entrySoftScore)),
    byMomentumWindowMs: stratumBy(trades, (t) =>
      t.momentumWindowMs === null || t.momentumWindowMs === undefined ? 'unknown' : String(t.momentumWindowMs),
    ),
    byHighVolatility: stratumBy(trades, (t) =>
      t.highVolatility === null ? 'unknown' : t.highVolatility ? 'hv' : 'normal',
    ),
    byHourUtc: stratumBy(trades, (t) => hourUtc(t.closedAt)),
  };

  const giveback = tp1Giveback(db, range);

  const sorted = [...trades].sort((a, b) => b.netPnlSol - a.netPnlSol);
  const examples = {
    bestTrades: sorted.slice(0, 10),
    worstTrades: [...sorted].reverse().slice(0, 10),
    emergencyExits: trades.filter((t) => t.exitReason === 'EMERGENCY_EXIT').slice(0, 15),
  };

  const hypotheses = buildHypotheses(trades, strata, stats, giveback, {
    avgLeftOnTable: avg(lefts),
    avgDetect: avg(detectMs),
    emergencyExitCount,
  });

  const caveats = [
    `Mode filter: ${allowMixed ? 'mixed allowed' : modeFilter ?? 'none'}. Live pilot n is often small — treat strata with underpowered=true as directional only.`,
    `Closed trades in range: ${trades.length}. Prefer config knobs over structural rewrites when n < 30.`,
    'Fees on paper are estimated; live fees depend on recorded fees_sol.',
    'Path marks / MFE timing only exist for trades collected after path instrumentation shipped.',
    'Do not mix conclusions across config_hash sessions without checking configSessions.',
  ];

  return {
    schemaVersion: STRATEGY_REPORT_SCHEMA_VERSION,
    generatedAt: new Date().toISOString(),
    range: { label: range, modeFilter: allowMixed ? null : modeFilter },
    configSessions: loadSessions(db),
    headline: {
      ...stats,
      leftOnTableAvgPct: avg(lefts),
      avgDetectToOpenMs: avg(detectMs),
      avgExitConfirmMs: avg(exitMs),
      emergencyExitCount,
      failedCount,
    },
    execution: {
      avgDetectToOpenMs: avg(detectMs),
      avgExitConfirmMs: avg(exitMs),
      totalFeesSol: stats.feesSol,
      failedCount,
      note: 'Separate execution drag (latency/fees/fails) from exit/selection edge before changing strategy code.',
    },
    strata,
    exitQuality: {
      avgMfePct: avg(mfe),
      avgMaePct: avg(mae),
      avgLeftOnTablePct: avg(lefts),
      tp1ThenGiveback: giveback,
    },
    selection: {
      funnel: getFunnelAnalytics(db, { range }),
      checkFailRates: listCheckFailRates(db, { range }),
      vetoReasons: getVetoReasonBreakdown(db, { range }),
    },
    examples,
    hypotheses,
    codeMap: CODE_MAP,
    caveats,
  };
}

export function renderStrategyWeekMarkdown(report: StrategyWeekReport): string {
  const h = report.headline;
  const lines: string[] = [
    '# Strategy Week Review',
    '',
    `Generated: ${report.generatedAt}  `,
    `Range: **${report.range.label}** · Mode filter: **${report.range.modeFilter ?? 'all'}** · Schema v${report.schemaVersion}`,
    '',
    '## Executive summary',
    '',
    `- Trades (n): **${h.tradeCount}** · Win rate: **${h.winRatePct.toFixed(1)}%** · Expectancy: **${h.expectancySol.toFixed(5)} SOL**`,
    `- Profit factor: **${h.profitFactor.toFixed(2)}** · Max DD: **${h.maxDrawdownSol.toFixed(4)} SOL** · Fees: **${h.feesSol.toFixed(4)} SOL**`,
    `- Left-on-table (avg MFE − realized %): **${h.leftOnTableAvgPct.toFixed(1)}%**`,
    `- Execution: detect→open p~avg **${h.avgDetectToOpenMs.toFixed(0)} ms**, exit confirm avg **${h.avgExitConfirmMs.toFixed(0)} ms**, emergencies **${h.emergencyExitCount}**, failed **${h.failedCount}**`,
    '',
    '## Config sessions',
    '',
  ];

  if (report.configSessions.length === 0) {
    lines.push('_No run_sessions recorded (pre-instrumentation)._', '');
  } else {
    for (const s of report.configSessions) {
      lines.push(
        `- session **${s.id}** hash=\`${s.configHash}\` mode=${s.mode} start=${s.startedAt}${s.gitCommit ? ` git=${s.gitCommit}` : ''}`,
      );
    }
    lines.push('');
  }

  lines.push('## Strata: exit reason', '', '| Reason | n | WR% | Exp SOL | PF | left-on-table% | underpowered |', '| --- | --- | --- | --- | --- | --- | --- |');
  for (const r of report.strata.byExitReason) {
    lines.push(
      `| ${r.key} | ${r.n} | ${r.winRatePct.toFixed(0)} | ${r.expectancySol.toFixed(4)} | ${r.profitFactor.toFixed(2)} | ${r.avgLeftOnTablePct.toFixed(1)} | ${r.underpowered} |`,
    );
  }

  lines.push('', '## Strata: soft-score bucket', '', '| Bucket | n | WR% | Exp SOL | PF | underpowered |', '| --- | --- | --- | --- | --- | --- |');
  for (const r of report.strata.byScoreBucket) {
    lines.push(
      `| ${r.key} | ${r.n} | ${r.winRatePct.toFixed(0)} | ${r.expectancySol.toFixed(4)} | ${r.profitFactor.toFixed(2)} | ${r.underpowered} |`,
    );
  }

  lines.push('', '## Strata: momentum window (ms)', '', '| Window | n | Exp SOL | PF | underpowered |', '| --- | --- | --- | --- | --- |');
  for (const r of report.strata.byMomentumWindowMs) {
    lines.push(`| ${r.key} | ${r.n} | ${r.expectancySol.toFixed(4)} | ${r.profitFactor.toFixed(2)} | ${r.underpowered} |`);
  }

  const funnel = report.selection.funnel;
  const veto = report.selection.vetoReasons;
  lines.push(
    '',
    '## Selection funnel',
    '',
    `- Graduations: **${funnel.graduations}** · Accepted: **${funnel.accepted}** · Vetoed: **${funnel.vetoed}** · Accept rate: **${funnel.acceptRatePct.toFixed(2)}%**`,
    `- Entered: **${funnel.entered}** · Closed: **${funnel.closed}** · Failed: **${funnel.failed}**`,
    '',
    '### Why candidates were vetoed (primary cause)',
    '',
    '| Reason | Category | n | % of vetoed |',
    '| --- | --- | --- | --- |',
  );
  if (veto.primary.length === 0) {
    lines.push('| _none_ | - | 0 | 0.0 |');
  } else {
    for (const r of veto.primary) {
      lines.push(`| ${r.reason} | ${r.category} | ${r.count} | ${r.pct.toFixed(1)} |`);
    }
  }
  lines.push(
    '',
    '_`unknown` = enrichment reliability (recoverable by budget/retry, NOT by relaxing safety); ' +
      '`hard_fail` = structural risk rejection; `low_score` = soft-score gate._',
  );

  lines.push('', '## Hypotheses (rule-based)', '');
  if (report.hypotheses.length === 0) {
    lines.push('_No hypothesis met sample thresholds. Collect more live trades or lower is not recommended — wait for n._', '');
  } else {
    for (const hyp of report.hypotheses) {
      lines.push(`### ${hyp.id}${hyp.underpowered ? ' _(underpowered)_' : ''}`);
      lines.push(`- **Claim:** ${hyp.claim}`);
      lines.push(`- **Evidence:** \`${JSON.stringify(hyp.evidence)}\``);
      lines.push(`- **Touchpoints:** ${hyp.suggestedTouchpoints.join(', ')}`);
      lines.push(`- **Suggested change:** ${hyp.suggestedChange}`);
      lines.push('');
    }
  }

  lines.push('## Code map', '');
  for (const [k, v] of Object.entries(report.codeMap)) {
    lines.push(`- **${k}:** \`${v}\``);
  }

  lines.push('', '## Worst trades (for qualitative review)', '');
  for (const t of report.examples.worstTrades.slice(0, 5)) {
    lines.push(
      `- \`${t.mint}\` net=${t.netPnlSol.toFixed(4)} exit=${t.exitReason} score=${t.entrySoftScore ?? '-'} MFE=${t.mfePct?.toFixed(1) ?? '-'}% left=${t.leftOnTablePct?.toFixed(1) ?? '-'}%`,
    );
  }

  lines.push('', '## Caveats', '');
  for (const c of report.caveats) lines.push(`- ${c}`);

  lines.push(
    '',
    '## Instructions for coding model',
    '',
    '1. Prefer **config.yaml** knob changes over large rewrites when n is small.',
    '2. Only act on hypotheses with `underpowered: false` unless explicitly exploring.',
    '3. Separate **execution** issues (latency/fees/fails) from **exit/selection** edge.',
    '4. Propose a minimal PR plan with files from codeMap; re-run this report after the next pilot week.',
    '',
  );

  return lines.join('\n');
}

export function strategyTradesCsv(trades: StrategyTradeRow[]): string {
  const headers = [
    'mint',
    'net_pnl_sol',
    'pnl_pct',
    'fees_sol',
    'exit_reason',
    'entry_soft_score',
    'high_volatility',
    'mfe_pct',
    'mae_pct',
    'left_on_table_pct',
    'hold_ms',
    'time_to_mfe_ms',
    'time_to_mae_ms',
    'detect_to_open_ms',
    'exit_confirm_ms',
    'momentum_window_ms',
    'early_flow_net_sol',
    'early_flow_rate',
    'pool_sol_at_entry',
    'size_multiplier',
    'mode',
    'config_hash',
    'opened_at',
    'closed_at',
    'entry_tx',
    'exit_tx',
  ];
  const lines = [headers.join(',')];
  for (const t of trades) {
    lines.push(
      [
        t.mint,
        t.netPnlSol,
        t.pnlPct,
        t.feesSol,
        t.exitReason,
        t.entrySoftScore,
        t.highVolatility,
        t.mfePct,
        t.maePct,
        t.leftOnTablePct,
        t.holdMs,
        t.timeToMfeMs,
        t.timeToMaeMs,
        t.detectToOpenMs,
        t.exitConfirmMs,
        t.momentumWindowMs,
        t.earlyFlowNetSol,
        t.earlyFlowRate,
        t.poolSolAtEntry,
        t.sizeMultiplier,
        t.mode,
        t.configHash,
        t.openedAt,
        t.closedAt,
        t.entryTx,
        t.exitTx,
      ]
        .map(csvEsc)
        .join(','),
    );
  }
  return lines.join('\n') + '\n';
}

export function strategyFillsCsv(db: DB, range: '24h' | '7d' | '30d' | 'all' = '7d'): string {
  const mod = rangeToModifier(range);
  const sql = mod
    ? `SELECT mint, session_id, trigger, fraction, price, gain_pct, pnl_sol, remaining_fraction, at_ms, created_at
       FROM position_fills
       WHERE julianday(created_at) >= julianday('now', ?)
       ORDER BY created_at ASC, id ASC`
    : `SELECT mint, session_id, trigger, fraction, price, gain_pct, pnl_sol, remaining_fraction, at_ms, created_at
       FROM position_fills ORDER BY created_at ASC, id ASC`;
  const rows = (mod ? db.prepare(sql).all(mod) : db.prepare(sql).all()) as Array<Record<string, unknown>>;
  const headers = [
    'mint',
    'session_id',
    'trigger',
    'fraction',
    'price',
    'gain_pct',
    'pnl_sol',
    'remaining_fraction',
    'at_ms',
    'created_at',
  ];
  const lines = [headers.join(',')];
  for (const r of rows) {
    lines.push(headers.map((h) => csvEsc(r[h])).join(','));
  }
  return lines.join('\n') + '\n';
}

/** Public trade loader for CSV / CLI exports. */
export function listStrategyTrades(
  db: DB,
  range: '24h' | '7d' | '30d' | 'all' = '7d',
  mode: string | null = null,
): StrategyTradeRow[] {
  return loadTrades(db, range, mode);
}

function loadTrades(db: DB, range: '24h' | '7d' | '30d' | 'all', mode: string | null): StrategyTradeRow[] {
  const mod = rangeToModifier(range);
  const modeClause = mode ? ` AND (mode = '${mode}' OR mode IS NULL)` : '';
  // mode IS NULL for legacy rows collected before mode denorm
  const sql = mod
    ? `SELECT mint,
              COALESCE(net_pnl_sol, pnl_sol, 0) AS netPnlSol,
              pnl_pct AS pnlPct,
              COALESCE(fees_sol, 0) AS feesSol,
              COALESCE(exit_reason, 'UNKNOWN') AS exitReason,
              entry_soft_score AS entrySoftScore,
              high_volatility AS highVolatility,
              mfe_pct AS mfePct,
              mae_pct AS maePct,
              left_on_table_pct AS leftOnTablePct,
              hold_ms AS holdMs,
              time_to_mfe_ms AS timeToMfeMs,
              time_to_mae_ms AS timeToMaeMs,
              detect_to_open_ms AS detectToOpenMs,
              exit_trigger_to_confirm_ms AS exitConfirmMs,
              momentum_window_ms AS momentumWindowMs,
              early_flow_net_sol AS earlyFlowNetSol,
              early_flow_rate AS earlyFlowRate,
              pool_sol_at_entry AS poolSolAtEntry,
              size_multiplier AS sizeMultiplier,
              mode, config_hash AS configHash,
              opened_at AS openedAt, closed_at AS closedAt,
              entry_tx AS entryTx, exit_tx AS exitTx,
              path_marks_json AS pathMarksJson,
              score_components_json AS scoreComponentsJson
       FROM positions
       WHERE state = 'CLOSED'
         AND julianday(COALESCE(closed_at, created_at)) >= julianday('now', ?)
         ${modeClause}
       ORDER BY julianday(COALESCE(closed_at, created_at)) ASC, rowid ASC`
    : `SELECT mint,
              COALESCE(net_pnl_sol, pnl_sol, 0) AS netPnlSol,
              pnl_pct AS pnlPct,
              COALESCE(fees_sol, 0) AS feesSol,
              COALESCE(exit_reason, 'UNKNOWN') AS exitReason,
              entry_soft_score AS entrySoftScore,
              high_volatility AS highVolatility,
              mfe_pct AS mfePct,
              mae_pct AS maePct,
              left_on_table_pct AS leftOnTablePct,
              hold_ms AS holdMs,
              time_to_mfe_ms AS timeToMfeMs,
              time_to_mae_ms AS timeToMaeMs,
              detect_to_open_ms AS detectToOpenMs,
              exit_trigger_to_confirm_ms AS exitConfirmMs,
              momentum_window_ms AS momentumWindowMs,
              early_flow_net_sol AS earlyFlowNetSol,
              early_flow_rate AS earlyFlowRate,
              pool_sol_at_entry AS poolSolAtEntry,
              size_multiplier AS sizeMultiplier,
              mode, config_hash AS configHash,
              opened_at AS openedAt, closed_at AS closedAt,
              entry_tx AS entryTx, exit_tx AS exitTx,
              path_marks_json AS pathMarksJson,
              score_components_json AS scoreComponentsJson
       FROM positions
       WHERE state = 'CLOSED'
         ${modeClause}
       ORDER BY julianday(COALESCE(closed_at, created_at)) ASC, rowid ASC`;

  const rows = (mod ? db.prepare(sql).all(mod) : db.prepare(sql).all()) as Array<Record<string, unknown>>;
  return rows.map((r) => ({
    mint: String(r.mint),
    netPnlSol: Number(r.netPnlSol ?? 0),
    pnlPct: numOrNull(r.pnlPct),
    feesSol: Number(r.feesSol ?? 0),
    exitReason: String(r.exitReason ?? 'UNKNOWN'),
    entrySoftScore: numOrNull(r.entrySoftScore),
    highVolatility: r.highVolatility === null || r.highVolatility === undefined ? null : Number(r.highVolatility) === 1,
    mfePct: numOrNull(r.mfePct),
    maePct: numOrNull(r.maePct),
    leftOnTablePct: numOrNull(r.leftOnTablePct),
    holdMs: numOrNull(r.holdMs),
    timeToMfeMs: numOrNull(r.timeToMfeMs),
    timeToMaeMs: numOrNull(r.timeToMaeMs),
    detectToOpenMs: numOrNull(r.detectToOpenMs),
    exitConfirmMs: numOrNull(r.exitConfirmMs),
    momentumWindowMs: numOrNull(r.momentumWindowMs),
    earlyFlowNetSol: numOrNull(r.earlyFlowNetSol),
    earlyFlowRate: numOrNull(r.earlyFlowRate),
    poolSolAtEntry: numOrNull(r.poolSolAtEntry),
    sizeMultiplier: numOrNull(r.sizeMultiplier),
    mode: r.mode === null || r.mode === undefined ? null : String(r.mode),
    configHash: r.configHash === null || r.configHash === undefined ? null : String(r.configHash),
    openedAt: r.openedAt === null || r.openedAt === undefined ? null : String(r.openedAt),
    closedAt: r.closedAt === null || r.closedAt === undefined ? null : String(r.closedAt),
    entryTx: r.entryTx === null || r.entryTx === undefined ? null : String(r.entryTx),
    exitTx: r.exitTx === null || r.exitTx === undefined ? null : String(r.exitTx),
    pathMarksJson: r.pathMarksJson === null || r.pathMarksJson === undefined ? null : String(r.pathMarksJson),
    scoreComponentsJson:
      r.scoreComponentsJson === null || r.scoreComponentsJson === undefined ? null : String(r.scoreComponentsJson),
  }));
}

function loadSessions(db: DB): StrategyWeekReport['configSessions'] {
  const rows = db
    .prepare(
      `SELECT id, started_at AS startedAt, ended_at AS endedAt, mode, config_hash AS configHash,
              git_commit AS gitCommit, config_json AS configJson
       FROM run_sessions ORDER BY id DESC LIMIT 20`,
    )
    .all() as Array<{
    id: number;
    startedAt: string;
    endedAt: string | null;
    mode: string;
    configHash: string;
    gitCommit: string | null;
    configJson: string;
  }>;
  return rows.map((r) => ({
    id: r.id,
    startedAt: r.startedAt,
    endedAt: r.endedAt,
    mode: r.mode,
    configHash: r.configHash,
    gitCommit: r.gitCommit,
    knobs: safeParse(r.configJson),
  }));
}

function countFailed(db: DB, range: '24h' | '7d' | '30d' | 'all', mode: string | null): number {
  const mod = rangeToModifier(range);
  const modeClause = mode ? ` AND (mode = '${mode}' OR mode IS NULL)` : '';
  const sql = mod
    ? `WITH latest AS (SELECT rowid AS id, * FROM positions WHERE rowid IN (SELECT MAX(rowid) FROM positions GROUP BY mint))
       SELECT COUNT(*) AS n FROM latest WHERE state = 'FAILED' AND julianday(created_at) >= julianday('now', ?) ${modeClause}`
    : `WITH latest AS (SELECT rowid AS id, * FROM positions WHERE rowid IN (SELECT MAX(rowid) FROM positions GROUP BY mint))
       SELECT COUNT(*) AS n FROM latest WHERE state = 'FAILED' ${modeClause}`;
  const row = (mod ? db.prepare(sql).get(mod) : db.prepare(sql).get()) as { n: number };
  return row.n;
}

function stratumBy(trades: StrategyTradeRow[], keyFn: (t: StrategyTradeRow) => string): StratumRow[] {
  const groups = new Map<string, StrategyTradeRow[]>();
  for (const t of trades) {
    const k = keyFn(t);
    const arr = groups.get(k) ?? [];
    arr.push(t);
    groups.set(k, arr);
  }
  const out: StratumRow[] = [];
  for (const [key, rows] of groups) {
    const stats = computePerformanceStats(rows.map((r) => r.netPnlSol));
    const lefts = rows.map((r) => r.leftOnTablePct).filter((x): x is number => x !== null);
    const mfes = rows.map((r) => r.mfePct).filter((x): x is number => x !== null);
    const maes = rows.map((r) => r.maePct).filter((x): x is number => x !== null);
    const holds = rows.map((r) => r.holdMs).filter((x): x is number => x !== null);
    out.push({
      key,
      n: rows.length,
      winRatePct: stats.winRatePct,
      expectancySol: stats.expectancySol,
      profitFactor: stats.profitFactor,
      avgMfePct: avg(mfes),
      avgMaePct: avg(maes),
      avgLeftOnTablePct: avg(lefts),
      avgHoldMs: avg(holds),
      realizedPnlSol: stats.realizedPnlSol,
      underpowered: rows.length < STRATA_N,
    });
  }
  return out.sort((a, b) => b.n - a.n);
}

function buildHypotheses(
  trades: StrategyTradeRow[],
  strata: StrategyWeekReport['strata'],
  stats: PerformanceStats,
  giveback: { n: number; avgGivebackPct: number },
  extra: { avgLeftOnTable: number; avgDetect: number; emergencyExitCount: number },
): StrategyHypothesis[] {
  const hyps: StrategyHypothesis[] = [];
  const n = trades.length;
  const globalUnder = n < GLOBAL_N;

  const trail = strata.byExitReason.find((s) => s.key === 'TRAILING_STOP');
  if (trail && trail.n >= 3 && trail.avgLeftOnTablePct > 15) {
    hyps.push({
      id: 'H-trail-left-on-table',
      claim: 'Trailing-stop exits leave substantial MFE on the table vs realized PnL.',
      evidence: { n: trail.n, avgLeftOnTablePct: trail.avgLeftOnTablePct, expectancySol: trail.expectancySol },
      suggestedTouchpoints: [CODE_MAP.exits, 'config.yaml#exits'],
      suggestedChange: 'Review trailingGapPct / trailingGapHighVolPct / trailingArmPct; consider slower trail or later arm.',
      underpowered: trail.n < STRATA_N,
    });
  }

  const stop = strata.byExitReason.find((s) => s.key === 'STOP_LOSS');
  if (stop && stop.n >= 3 && stop.expectancySol < -0.001) {
    hyps.push({
      id: 'H-stop-loss-drag',
      claim: 'Hard stop exits are a material expectancy drag.',
      evidence: { n: stop.n, expectancySol: stop.expectancySol, avgMaePct: stop.avgMaePct },
      suggestedTouchpoints: [CODE_MAP.exits, 'config.yaml#exits.hardStopPct'],
      suggestedChange: 'Check if hardStopPct is too tight for live volatility; compare MAE distribution.',
      underpowered: stop.n < STRATA_N,
    });
  }

  const lowScore = strata.byScoreBucket.find((s) => s.key === '60-70');
  const highScore = strata.byScoreBucket.find((s) => s.key === '80-90' || s.key === '90+');
  if (lowScore && highScore && lowScore.n >= 3 && highScore.n >= 3 && lowScore.expectancySol + 0.002 < highScore.expectancySol) {
    hyps.push({
      id: 'H-raise-min-score',
      claim: 'Lower soft-score buckets underperform higher buckets — minEntryScore may be too low.',
      evidence: {
        lowN: lowScore.n,
        lowExp: lowScore.expectancySol,
        highN: highScore.n,
        highExp: highScore.expectancySol,
      },
      suggestedTouchpoints: [CODE_MAP.entry, CODE_MAP.scoring],
      suggestedChange: 'Consider raising entry.minEntryScore or flattening size at 60–70.',
      underpowered: lowScore.n < STRATA_N || highScore.n < STRATA_N,
    });
  }

  if (extra.avgLeftOnTable > 20 && n >= 5) {
    hyps.push({
      id: 'H-exit-quality',
      claim: 'Average left-on-table is high — exit ladder may cut winners early.',
      evidence: { n, avgLeftOnTablePct: extra.avgLeftOnTable, globalExpectancy: stats.expectancySol },
      suggestedTouchpoints: [CODE_MAP.exits],
      suggestedChange: 'Review tp1SellFraction / tp2Pct / trailing; inspect path_marks on best vs worst trades.',
      underpowered: globalUnder,
    });
  }

  if (extra.emergencyExitCount >= 3) {
    hyps.push({
      id: 'H-emergency-rate',
      claim: 'Multiple EMERGENCY_EXIT events in the window — LP/creator monitors or selection may be weak.',
      evidence: { emergencyExitCount: extra.emergencyExitCount, n },
      suggestedTouchpoints: [CODE_MAP.guardrails, CODE_MAP.risk, 'src/positions/monitors.ts'],
      suggestedChange: 'Review emergencyLpDropPct / creatorDumpThresholdPct and pre-entry holder/creator caps.',
      underpowered: extra.emergencyExitCount < STRATA_N,
    });
  }

  if (giveback.n >= 3 && giveback.avgGivebackPct > 10) {
    hyps.push({
      id: 'H-tp1-giveback',
      claim: 'After TP1, remainder often gives back gains before final exit.',
      evidence: { n: giveback.n, avgGivebackPct: giveback.avgGivebackPct },
      suggestedTouchpoints: [CODE_MAP.exits, 'config.yaml#exits.tp1MoveStopToPct'],
      suggestedChange: 'Tighten post-TP1 stop (tp1MoveStopToPct) or increase tp1SellFraction.',
      underpowered: giveback.n < STRATA_N,
    });
  }

  if (extra.avgDetect > 2000 && n >= 5) {
    hyps.push({
      id: 'H-detect-to-open-slow',
      claim: 'Detect→open latency is high for a graduation scalper — edge may be execution not signals.',
      evidence: { avgDetectToOpenMs: extra.avgDetect, n },
      suggestedTouchpoints: ['src/detector/', 'src/enrichment/', 'config.yaml#guardrails.enrichmentBudgetMs'],
      suggestedChange: 'Profile enrichment budget / confirmation path before retuning scores.',
      underpowered: globalUnder,
    });
  }

  return hyps;
}

function tp1Giveback(db: DB, range: '24h' | '7d' | '30d' | 'all'): { n: number; avgGivebackPct: number } {
  const mod = rangeToModifier(range);
  // Mints that had TP1 fill and a later non-TP1 fill
  const sql = mod
    ? `SELECT mint FROM position_fills
       WHERE trigger = 'TAKE_PROFIT_1' AND julianday(created_at) >= julianday('now', ?)
       GROUP BY mint`
    : `SELECT mint FROM position_fills WHERE trigger = 'TAKE_PROFIT_1' GROUP BY mint`;
  const mints = (mod ? db.prepare(sql).all(mod) : db.prepare(sql).all()) as Array<{ mint: string }>;
  if (mints.length === 0) return { n: 0, avgGivebackPct: 0 };

  let sum = 0;
  let n = 0;
  for (const { mint } of mints) {
    const fills = db
      .prepare(
        `SELECT trigger, gain_pct FROM position_fills WHERE mint = ? ORDER BY id ASC`,
      )
      .all(mint) as Array<{ trigger: string; gain_pct: number | null }>;
    const tp1 = fills.find((f) => f.trigger === 'TAKE_PROFIT_1');
    const last = fills[fills.length - 1];
    if (!tp1 || !last || last.trigger === 'TAKE_PROFIT_1') continue;
    if (tp1.gain_pct === null || last.gain_pct === null) continue;
    const give = tp1.gain_pct - last.gain_pct;
    if (give > 0) {
      sum += give;
      n += 1;
    }
  }
  return { n, avgGivebackPct: n > 0 ? sum / n : 0 };
}

function scoreBucket(score: number | null): string {
  if (score === null || !Number.isFinite(score)) return 'unknown';
  if (score < 60) return '<60';
  if (score < 70) return '60-70';
  if (score < 80) return '70-80';
  if (score < 90) return '80-90';
  return '90+';
}

function hourUtc(closedAt: string | null): string {
  if (!closedAt) return 'unknown';
  const d = new Date(closedAt.includes('T') ? closedAt : `${closedAt.replace(' ', 'T')}Z`);
  if (Number.isNaN(d.getTime())) return 'unknown';
  return `${d.getUTCHours().toString().padStart(2, '0')}:00Z`;
}

function avg(xs: number[]): number {
  if (xs.length === 0) return 0;
  return xs.reduce((a, b) => a + b, 0) / xs.length;
}

function numOrNull(v: unknown): number | null {
  return typeof v === 'number' && Number.isFinite(v) ? v : null;
}

function csvEsc(v: unknown): string {
  if (v === null || v === undefined) return '';
  const s = String(v);
  if (/[",\n\r]/.test(s)) return `"${s.replace(/"/g, '""')}"`;
  return s;
}

function safeParse(text: string): unknown {
  try {
    return JSON.parse(text);
  } catch {
    return null;
  }
}
