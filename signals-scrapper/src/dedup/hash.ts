import { createHash } from 'crypto';
import { IdeaHashInput } from '../models/trading-idea.model';

/**
 * Stable dedup hash for a trading idea.
 * hash = sha256(provider + instrument + timeframe + ideaTimestamp + direction + target)
 *
 * When the provider timestamp is present, retain the legacy formula exactly so
 * existing seen.json hashes remain valid. OCR may legitimately miss a
 * timestamp; that branch uses the stable stop-loss/take-profit values and never
 * the changing current-price/entry marker.
 */
export function computeIdeaHash(idea: IdeaHashInput): string {
  const takeProfit = idea.takeProfit ?? idea.target;
  const targetPart = takeProfit == null ? '' : String(takeProfit);
  const payload = idea.ideaTimestamp
    ? [
        idea.provider,
        idea.instrument,
        idea.timeframe,
        idea.ideaTimestamp,
        idea.direction,
        targetPart,
      ].join('|')
    : [
        idea.provider,
        idea.instrument,
        idea.timeframe,
        'NO_TIMESTAMP',
        idea.direction,
        String(idea.stopLoss ?? idea.pivot ?? ''),
        targetPart,
      ].join('|');
  return createHash('sha256').update(payload, 'utf8').digest('hex');
}
