import type { Config } from '../config/schema.ts';
import type { Question } from './types.ts';

/**
 * The entry battery: atomic questions, each evaluated in isolation against the
 * same state, recombined by code (policy.ts) with explicit thresholds. When
 * priorities change, edit a threshold, not a prompt. Bump the version on ANY
 * prompt change — calibration is per (model version, question set version).
 *
 * `continuation` asks exactly what the learned filter's label measures (triple
 * barrier at the configured TP1 / hard stop / time stop, after costs), so its
 * reliability is directly comparable to model_prob on the same rows.
 */
export const ENTRY_QUESTION_SET_VERSION = 1;

export const ENTRY_QUESTION_IDS = ['continuation', 'toxic_flow', 'rug_risk', 'manipulation', 'flow_regime', 'setup_quality'] as const;

export function buildEntryQuestions(config: Config): Question[] {
  const tp = config.exits.tp1Pct;
  const sl = config.exits.hardStopPct;
  const horizonMin = config.exits.timeStopMinutes;
  return [
    {
      id: 'continuation',
      kind: 'probability',
      prompt:
        `A pump.fun token just graduated to a PumpSwap pool. If we buy now, what is the probability the trade is ` +
        `profitable after ~3% round-trip costs, exiting at +${tp}% take-profit, -${sl}% stop, or after ${horizonMin} minutes, whichever comes first?`,
    },
    {
      id: 'toxic_flow',
      kind: 'probability',
      prompt: 'Probability the early post-graduation order flow is dominated by informed or insider selling into new buyers.',
    },
    {
      id: 'rug_risk',
      kind: 'probability',
      prompt: 'Probability of a liquidity pull, creator dump, or a >50% crash within the next 5 minutes.',
    },
    {
      id: 'manipulation',
      kind: 'probability',
      prompt: 'Probability this launch was coordinated: bundled buys, wash trading, or a sniper/insider cluster.',
    },
    {
      id: 'flow_regime',
      kind: 'choice',
      prompt: 'Which best describes the early post-graduation flow?',
      options: ['accumulation', 'distribution', 'churn', 'thin'],
    },
    {
      id: 'setup_quality',
      kind: 'score',
      prompt: 'Quality of this setup for a short momentum scalp.',
      min: 0,
      max: 3,
      rubric: ['avoid', 'weak', 'acceptable', 'strong'],
    },
  ];
}
