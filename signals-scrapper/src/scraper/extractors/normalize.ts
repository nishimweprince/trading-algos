import { computeIdeaHash } from '../../dedup/hash';
import {
  Direction,
  ProviderType,
  TradingIdea,
} from '../../models/trading-idea.model';

export interface PartialIdea {
  instrument: string;
  timeframe: string;
  direction: Direction | string;
  expectedMovePips?: [number, number];
  target?: number;
  pivot?: number;
  levels?: { support: number[]; resistance: number[] };
  ideaTimestamp: string;
  raw?: unknown;
}

export function normalizeDirection(value: unknown): Direction {
  if (value === null || value === undefined) return 'NEUTRAL';
  const raw = String(value).trim();
  // Trading Central uses arrow glyphs next to Expected Move
  if (/[↓▼🡇⬇]/.test(raw)) return 'DOWN';
  if (/[↑▲🡅⬆]/.test(raw)) return 'UP';
  const s = raw.toUpperCase();
  if (
    s === 'UP' ||
    s === 'LONG' ||
    s === 'BUY' ||
    s === 'BULLISH' ||
    s.includes('UP') ||
    s.includes('BULL')
  ) {
    return 'UP';
  }
  if (
    s === 'DOWN' ||
    s === 'SHORT' ||
    s === 'SELL' ||
    s === 'BEARISH' ||
    s.includes('DOWN') ||
    s.includes('BEAR')
  ) {
    return 'DOWN';
  }
  return 'NEUTRAL';
}

export function toIsoTimestamp(value: unknown, fallback: string): string {
  if (value === null || value === undefined || value === '') return fallback;
  if (typeof value === 'number') {
    // Assume ms if large, else seconds
    const ms = value > 1e12 ? value : value * 1000;
    return new Date(ms).toISOString();
  }
  let s = String(value).trim();
  // "Friday, July 10, 2026 7:14:53 AM CET" — drop weekday + map common TZ abbrev
  s = s.replace(
    /^(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+/i,
    '',
  );
  s = s
    .replace(/\bCET\b/g, 'GMT+0100')
    .replace(/\bCEST\b/g, 'GMT+0200')
    .replace(/\bUTC([+-]\d{1,2})\b/g, 'GMT$1');
  const d = new Date(s);
  if (!Number.isNaN(d.getTime())) return d.toISOString();
  // Retry without timezone token
  const stripped = s.replace(/\s+(GMT[+-]\d{1,4}|[A-Z]{2,5})$/i, '');
  const d2 = new Date(stripped);
  if (!Number.isNaN(d2.getTime())) return d2.toISOString();
  return fallback;
}

export function parseNumber(value: unknown): number | undefined {
  if (value === null || value === undefined || value === '') return undefined;
  if (typeof value === 'number' && Number.isFinite(value)) return value;
  const n = Number(String(value).replace(/[, ]/g, ''));
  return Number.isFinite(n) ? n : undefined;
}

/**
 * Map a partial idea + context into the shared TradingIdea shape with hash.
 */
export function normalizeIdea(
  partial: PartialIdea,
  opts: {
    provider: ProviderType;
    sourceUrl: string;
    capturedAt: string;
    screenshotPath?: string;
  },
): TradingIdea {
  const direction = normalizeDirection(partial.direction);
  const ideaTimestamp = toIsoTimestamp(partial.ideaTimestamp, opts.capturedAt);
  const base = {
    provider: opts.provider,
    instrument: String(partial.instrument).trim(),
    timeframe: String(partial.timeframe).trim(),
    direction,
    expectedMovePips: partial.expectedMovePips,
    target: partial.target,
    pivot: partial.pivot,
    levels: partial.levels,
    ideaTimestamp,
    capturedAt: opts.capturedAt,
    sourceUrl: opts.sourceUrl,
    screenshotPath: opts.screenshotPath,
    raw: partial.raw,
  };
  const hash = computeIdeaHash(base);
  return { ...base, hash };
}
