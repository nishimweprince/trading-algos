import {
  normalizeDirection,
  normalizeIdea,
  parseNumber,
  toIsoTimestamp,
} from './normalize';
import { TradingIdea } from '../../models/trading-idea.model';
import { ExtractContext } from '../idea-extractor.interface';

/**
 * Parse Trading Central / Recognia card text as shown in the live widget:
 *
 *   USD/CAD  30 MIN          12:14 AM (UTC-5)
 *   Friday, July 10, 2026 7:14:53 AM CET
 *   Expected Move   ↓ 19 - 39 PIPS
 *   Target          1.4115
 *   Pivot           1.4170
 *   [Trade]
 */

/** Non-global patterns — never use .test() on a /g regex (lastIndex skips cards). */
const INSTRUMENT_SRC =
  '([A-Z]{3}\\s*\\/\\s*[A-Z]{3}|XAU\\s*\\/\\s*USD|XAG\\s*\\/\\s*USD|NAS100|US30|US500|SPX500|UK100|GER40|JP225)';
const INSTRUMENT_RE = new RegExp(`\\b${INSTRUMENT_SRC}\\b`);
const INSTRUMENT_RE_G = new RegExp(`\\b${INSTRUMENT_SRC}\\b`, 'g');

const TIMEFRAME_RE =
  /\b(\d+\s*(?:MIN|MINS|MINUTE|MINUTES|H|HR|HRS|HOUR|HOURS|D|DAY|DAYS|W|WEEK|WEEKS)|INTRADAY|ST|MT|LT)\b/i;

const DIR_ARROW_RE = /([↓↑▼▲🡇🡅⬇⬆]|DOWN|UP|BEARISH|BULLISH|SHORT|LONG|SELL|BUY)/i;

const PIPS_RE =
  /(?:Expected\s*Move|Exp(?:ected)?\.?\s*Move)?[^\n\d]*?([↓↑▼▲🡇🡅⬇⬆])?\s*(\d+)\s*[-–—to]+\s*(\d+)\s*PIPS?/i;

const TARGET_RE = /\bTarget\s*[:.]?\s*([\d]+(?:[.,]\d+)?)/i;
const PIVOT_RE = /\bPivot\s*[:.]?\s*([\d]+(?:[.,]\d+)?)/i;

const LONG_DATE_RE =
  /\b(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+[A-Z][a-z]+\s+\d{1,2},?\s+\d{4}\s+\d{1,2}:\d{2}(?::\d{2})?\s*(?:AM|PM)?(?:\s*[A-Z]{2,5})?\b/i;

const SHORT_TIME_RE = /\b(\d{1,2}:\d{2}(?::\d{2})?\s*(?:AM|PM)\s*\([^)]+\))/i;

function hasInstrument(s: string): boolean {
  return INSTRUMENT_RE.test(s);
}

function hasSignalFields(s: string): boolean {
  return TARGET_RE.test(s) || PIPS_RE.test(s) || PIVOT_RE.test(s);
}

export function normalizeInstrument(raw: string): string {
  return raw.replace(/\s+/g, '').toUpperCase();
}

export function normalizeTimeframe(raw: string): string {
  return raw.replace(/\s+/g, ' ').trim().toUpperCase();
}

/**
 * Split widget body text into per-card chunks.
 * Prefer splitting on the green "Trade" button label that ends each card.
 */
export function splitTradingCentralCards(text: string): string[] {
  const normalized = text
    .replace(/\u00a0/g, ' ')
    .replace(/[ \t]+/g, ' ')
    .replace(/\r\n/g, '\n');

  const byTrade = normalized
    .split(/\bTrade\b/i)
    .map((s) => s.trim())
    .filter((s) => hasInstrument(s) && hasSignalFields(s));

  if (byTrade.length > 0) return byTrade;

  // Fallback: split on instrument headers
  const chunks: string[] = [];
  const matches = [...normalized.matchAll(INSTRUMENT_RE_G)];
  for (let i = 0; i < matches.length; i++) {
    const start = matches[i].index ?? 0;
    const end =
      i + 1 < matches.length
        ? (matches[i + 1].index ?? start + 400)
        : Math.min(normalized.length, start + 400);
    const chunk = normalized.slice(start, end).trim();
    if (chunk.length > 10 && hasSignalFields(chunk)) chunks.push(chunk);
  }
  return chunks;
}

/**
 * Parse one card chunk into a TradingIdea, or null if essential fields missing.
 */
export function parseTradingCentralCardChunk(
  chunk: string,
  ctx: ExtractContext,
): TradingIdea | null {
  const instMatch = chunk.match(INSTRUMENT_RE);
  if (!instMatch) return null;
  const instrument = normalizeInstrument(instMatch[1]);

  const afterInst = chunk.slice((instMatch.index ?? 0) + instMatch[0].length);
  const tfMatch = afterInst.match(TIMEFRAME_RE) ?? chunk.match(TIMEFRAME_RE);
  const timeframe = tfMatch ? normalizeTimeframe(tfMatch[1]) : 'UNKNOWN';

  let expectedMovePips: [number, number] | undefined;
  let directionRaw = 'NEUTRAL';
  const pipsMatch = chunk.match(PIPS_RE);
  if (pipsMatch) {
    if (pipsMatch[1]) directionRaw = pipsMatch[1];
    const a = parseNumber(pipsMatch[2]);
    const b = parseNumber(pipsMatch[3]);
    if (a !== undefined && b !== undefined) expectedMovePips = [a, b];
  }
  if (directionRaw === 'NEUTRAL') {
    const dirMatch = chunk.match(DIR_ARROW_RE);
    if (dirMatch) directionRaw = dirMatch[1];
  }
  if (/[↓▼🡇⬇]/.test(directionRaw)) directionRaw = 'DOWN';
  if (/[↑▲🡅⬆]/.test(directionRaw)) directionRaw = 'UP';

  const target = parseNumber(chunk.match(TARGET_RE)?.[1]?.replace(',', '.'));
  const pivot = parseNumber(chunk.match(PIVOT_RE)?.[1]?.replace(',', '.'));

  if (target === undefined && pivot === undefined && !expectedMovePips) {
    return null;
  }

  const longDate = chunk.match(LONG_DATE_RE)?.[0];
  const shortTime = chunk.match(SHORT_TIME_RE)?.[1];
  const ideaTimestamp = toIsoTimestamp(
    longDate ?? shortTime ?? '',
    ctx.capturedAt,
  );

  return normalizeIdea(
    {
      instrument,
      timeframe,
      direction: directionRaw,
      expectedMovePips,
      target,
      pivot,
      ideaTimestamp,
      raw: { textSnippet: chunk.replace(/\s+/g, ' ').trim().slice(0, 400) },
    },
    {
      provider: 'TRADING_CENTRAL',
      sourceUrl: ctx.sourceUrl,
      capturedAt: ctx.capturedAt,
      screenshotPath: ctx.screenshotPath,
    },
  );
}

/**
 * Parse full widget/page text into TradingIdea[].
 * This is the primary path for the live Recognia iframe body text.
 */
export function parseTradingCentralText(
  text: string,
  ctx: ExtractContext,
): TradingIdea[] {
  if (!text || !text.trim()) return [];
  const chunks = splitTradingCentralCards(text);
  const ideas: TradingIdea[] = [];
  const seen = new Set<string>();
  for (const chunk of chunks) {
    const idea = parseTradingCentralCardChunk(chunk, ctx);
    if (!idea) continue;
    if (seen.has(idea.hash)) continue;
    seen.add(idea.hash);
    ideas.push(idea);
  }
  return ideas;
}

export function directionFromArrow(value: string): 'UP' | 'DOWN' | 'NEUTRAL' {
  if (/[↓▼🡇⬇]|DOWN|BEAR|SELL|SHORT/i.test(value)) return 'DOWN';
  if (/[↑▲🡅⬆]|UP|BULL|BUY|LONG/i.test(value)) return 'UP';
  return normalizeDirection(value);
}
