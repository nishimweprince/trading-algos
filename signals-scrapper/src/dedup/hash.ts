import { createHash } from 'crypto';
import { IdeaHashInput } from '../models/trading-idea.model';

/**
 * Stable dedup hash for a trading idea.
 * hash = sha256(provider + instrument + timeframe + ideaTimestamp + direction + target)
 *
 * Include target and direction so a genuinely updated idea on the same
 * instrument/timeframe counts as new.
 */
export function computeIdeaHash(idea: IdeaHashInput): string {
  const targetPart =
    idea.target === undefined || idea.target === null ? '' : String(idea.target);
  const payload = [
    idea.provider,
    idea.instrument,
    idea.timeframe,
    idea.ideaTimestamp,
    idea.direction,
    targetPart,
  ].join('|');
  return createHash('sha256').update(payload, 'utf8').digest('hex');
}
