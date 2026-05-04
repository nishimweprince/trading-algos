import { v4 as uuidv4 } from 'uuid';
import type { ChannelParseConfig, ParsedMessage, Signal } from './types.js';

const ACTION = /\b(buy|sell)\b/gi;
const INSTRUMENT = /\b(gold|xau\/?usd|xau)\b/gi;

const INSTRUMENT_MAP: Record<string, 'XAU'> = {
  gold: 'XAU',
  xau: 'XAU',
  'xau/usd': 'XAU',
  xauusd: 'XAU',
};

const PROXIMITY_CHARS = 40;

function normalizeInstrumentKey(match: string): string {
  return match.toLowerCase().replace(/\s+/g, '').replace(/\//g, '');
}

function findInstrument(textLower: string): { key: string; symbol: 'XAU'; index: number } | null {
  INSTRUMENT.lastIndex = 0;
  let m: RegExpExecArray | null;
  while ((m = INSTRUMENT.exec(textLower)) !== null) {
    const key = normalizeInstrumentKey(m[0]);
    const symbol = INSTRUMENT_MAP[key];
    if (symbol) return { key, symbol, index: m.index };
  }
  return null;
}

function findActions(textLower: string): { word: 'buy' | 'sell'; index: number }[] {
  const out: { word: 'buy' | 'sell'; index: number }[] = [];
  ACTION.lastIndex = 0;
  let m: RegExpExecArray | null;
  while ((m = ACTION.exec(textLower)) !== null) {
    const w = m[1].toLowerCase();
    if (w === 'buy' || w === 'sell') out.push({ word: w, index: m.index });
  }
  return out;
}

export function parseMessageText(
  text: string | undefined,
  ctx: {
    sourceChannel: string;
    messageId: number;
    messageDate: string;
  },
  _channelConfig?: ChannelParseConfig,
): ParsedMessage {
  if (text === undefined || text === null || text.trim() === '') {
    return { isSignal: false, reason: 'empty_text' };
  }

  const rawText = text;
  const textLower = text.trim().toLowerCase();

  const inst = findInstrument(textLower);
  if (!inst) {
    return { isSignal: false, reason: 'unknown_instrument' };
  }

  const actions = findActions(textLower);
  const buys = actions.filter((a) => a.word === 'buy');
  const sells = actions.filter((a) => a.word === 'sell');
  if (buys.length > 0 && sells.length > 0) {
    return { isSignal: false, reason: 'ambiguous_direction' };
  }
  if (actions.length === 0) {
    return { isSignal: false, reason: 'no_action' };
  }
  if (buys.length > 1 || sells.length > 1) {
    return { isSignal: false, reason: 'ambiguous_direction' };
  }

  const action = actions[0];
  const dist = Math.abs(action.index - inst.index);
  if (dist > PROXIMITY_CHARS) {
    return { isSignal: false, reason: 'action_too_far_from_instrument' };
  }

  return {
    isSignal: true,
    symbol: inst.symbol,
    direction: action.word === 'buy' ? 'BUY' : 'SELL',
    sourceChannel: ctx.sourceChannel,
    messageId: ctx.messageId,
    messageDate: ctx.messageDate,
    rawText,
  };
}

export function parsedToSignal(parsed: Extract<ParsedMessage, { isSignal: true }>, channelId: string): Signal {
  return {
    id: uuidv4(),
    symbol: parsed.symbol,
    direction: parsed.direction,
    sourceChannel: parsed.sourceChannel,
    messageId: parsed.messageId,
    messageDate: parsed.messageDate,
    rawText: parsed.rawText,
    channelId,
  };
}
