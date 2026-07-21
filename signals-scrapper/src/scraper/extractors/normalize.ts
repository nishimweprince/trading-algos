import { computeIdeaHash } from '../../dedup/hash';
import {
  Direction,
  ProviderType,
  TradingIdea,
} from '../../models/trading-idea.model';
import { ResolvedHostOs } from '../../config/sources.schema';

const PAIR_ASSET_CODES = new Set([
  'USD',
  'EUR',
  'GBP',
  'JPY',
  'CHF',
  'CAD',
  'AUD',
  'NZD',
  'XAU',
  'XAG',
  'BTC',
  'ETH',
  'LTC',
  'XRP',
  'BCH',
]);

/**
 * Autochartist card headers show FX pairs as one compact 6-letter token
 * (e.g. "GBPDKK", "USDZAR"). MT5_SIGNAL_RULES are keyed by AAA/BBB, so split
 * a 6-letter uppercase symbol into that form. Non 6-letter symbols (indices,
 * metals already slashed, etc.) are returned trimmed/upper-cased unchanged.
 */
export function splitPairSymbol(raw: string | null | undefined): string {
  const normalized = raw?.trim().toUpperCase() ?? '';
  if (normalized.includes('/')) return normalized;
  const compact = normalized.replace(/\s+/g, '');
  const sixLetter = compact.match(/^([A-Z]{3})([A-Z]{3})$/);
  if (!sixLetter) return normalized;
  return `${sixLetter[1]}/${sixLetter[2]}`;
}

/** Correct the Windows screenshot artifact where a currency-pair slash is read as I. */
export function normalizeVisionInstrument(
  raw: string | null | undefined,
  hostOs: ResolvedHostOs | undefined,
): string {
  const normalized = raw?.trim().toUpperCase() ?? '';
  if (hostOs !== 'WINDOWS') return normalized;

  const compact = normalized.replace(/\s+/g, '');
  const mistakenSeparator = compact.match(/^([A-Z]{3})I([A-Z]{3})$/);
  if (!mistakenSeparator) return normalized;

  const [, base, quote] = mistakenSeparator;
  if (!PAIR_ASSET_CODES.has(base) || !PAIR_ASSET_CODES.has(quote)) {
    return normalized;
  }
  return `${base}/${quote}`;
}

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

/**
 * Autochartist cards show identified times as "M/D HH:MM" with no year or
 * timezone (e.g. "7/21 15:00"). Dedup requires a value that is STABLE across
 * runs for the same signal, so we build a deterministic ISO from the compact
 * text using the capturedAt year (UTC). Full/parseable dates fall through to
 * toIsoTimestamp. Returns null when nothing usable is present, letting the
 * hash fall back to the stable price-level branch.
 */
export function autochartistIdeaTimestamp(
  text: string | null | undefined,
  capturedAt: string,
): string | null {
  if (!text || text.trim() === '') return null;
  // Handle the compact "M/D HH:MM" (optionally "M/D/YYYY") form first — bare
  // dates otherwise parse to an engine-default year (e.g. 2001), not the year
  // the signal was captured in.
  const md = text.match(
    /(\d{1,2})\/(\d{1,2})(?:\/(\d{2,4}))?\s+(\d{1,2}):(\d{2})/,
  );
  if (md) {
    const [, month, day, yearRaw, hour, minute] = md;
    const captured = new Date(capturedAt);
    const baseYear = Number.isNaN(captured.getTime())
      ? new Date().getUTCFullYear()
      : captured.getUTCFullYear();
    let year = yearRaw
      ? Number(yearRaw.length === 2 ? `20${yearRaw}` : yearRaw)
      : baseYear;
    let ms = Date.UTC(
      year,
      Number(month) - 1,
      Number(day),
      Number(hour),
      Number(minute),
    );
    // Guard the Dec→Jan rollover: an identified time far in the "future" of
    // capturedAt actually belongs to the previous year.
    if (
      !yearRaw &&
      !Number.isNaN(captured.getTime()) &&
      ms - captured.getTime() > 3 * 86_400_000
    ) {
      year -= 1;
      ms = Date.UTC(
        year,
        Number(month) - 1,
        Number(day),
        Number(hour),
        Number(minute),
      );
    }
    return Number.isNaN(ms) ? null : new Date(ms).toISOString();
  }
  // Fuller/ISO date strings (with year + optional timezone).
  return toIsoTimestamp(text, '') || null;
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
