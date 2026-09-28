/**
 * Metadata battery research (shadow): does any text judgment separate
 * outcomes? For each probability question the labelled canonical candidates
 * are split at 0.5; for `narrative`, per chosen option. Win rate and mean net
 * return per bucket, with a bootstrap CI on the mean — a question earns a
 * place in the soft score / learned filter only when the buckets' CIs part.
 */
import type { DecisionRow, MintLabel } from './decisionReport.ts';
import { bootstrapCI } from './stats.ts';
import { METADATA_QUESTION_IDS } from '../decision/metadataQuestions.ts';

export interface MetadataBucket {
  question: string;
  bucket: string;
  n: number;
  winRate: number;
  meanNetReturn: number;
  lo: number;
  hi: number;
}

export function buildMetadataReport(rows: readonly DecisionRow[], labels: ReadonlyMap<string, MintLabel>, opts: { seed: number }): {
  scored: number;
  labelled: number;
  buckets: MetadataBucket[];
} {
  const groups = new Map<string, MintLabel[]>();
  let labelled = 0;
  for (const r of rows) {
    const label = labels.get(r.mint);
    if (!label) continue;
    labelled++;
    for (const a of r.answers) {
      if (!(METADATA_QUESTION_IDS as readonly string[]).includes(a.id)) continue;
      const bucket = typeof a.value === 'number' ? (a.value >= 0.5 ? '>= 0.5' : '< 0.5') : String(a.value);
      const key = `${a.id}\u0000${bucket}`;
      const list = groups.get(key) ?? [];
      list.push(label);
      groups.set(key, list);
    }
  }
  const buckets: MetadataBucket[] = [];
  for (const [key, list] of groups) {
    const [question, bucket] = key.split('\u0000') as [string, string];
    const returns = list.map((l) => l.netReturn);
    const ci = bootstrapCI(returns, { seed: opts.seed, iterations: 2_000 });
    buckets.push({
      question,
      bucket,
      n: list.length,
      winRate: list.filter((l) => l.label === 1).length / list.length,
      meanNetReturn: ci.point,
      lo: ci.lo,
      hi: ci.hi,
    });
  }
  const order = (q: string) => (METADATA_QUESTION_IDS as readonly string[]).indexOf(q);
  buckets.sort((a, b) => order(a.question) - order(b.question) || a.bucket.localeCompare(b.bucket));
  return { scored: rows.length, labelled, buckets };
}

export function renderMetadataReport(r: ReturnType<typeof buildMetadataReport>, ctx: { db: string }): string {
  const pct = (x: number) => (Number.isFinite(x) ? `${(x * 100).toFixed(1)}%` : '—');
  const lines = [
    '# Jev metadata battery — shadow report',
    '',
    `DB: \`${ctx.db}\` · scored mints: ${r.scored} · with a canonical label: ${r.labelled}`,
    '',
    'A question is worth using only when its buckets\' mean-return CIs do not overlap (aim for >= ~300 labelled).',
    '',
    '| Question | Answer | n | Win rate | Mean net return [95% CI] |',
    '|---|---|---|---|---|',
  ];
  for (const b of r.buckets) {
    lines.push(`| ${b.question} | ${b.bucket} | ${b.n} | ${pct(b.winRate)} | ${pct(b.meanNetReturn)} [${pct(b.lo)}, ${pct(b.hi)}] |`);
  }
  if (r.buckets.length === 0) lines.push('| — | no labelled metadata calls yet | 0 | — | — |');
  return lines.join('\n') + '\n';
}
