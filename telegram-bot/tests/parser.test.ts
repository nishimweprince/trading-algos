import { describe, it, expect } from 'vitest';
import { parseMessageText } from '../src/signals/parser.js';
import { FIXTURE_MESSAGES } from './fixtures/messages.js';

const ctx = (channel = '@test') => ({
  sourceChannel: channel,
  messageId: 1,
  messageDate: '2026-05-04T12:00:00.000Z',
});

describe('parseMessageText', () => {
  it('detects Gold buy now', () => {
    const r = parseMessageText(FIXTURE_MESSAGES.goldBuyNow, ctx());
    expect(r.isSignal && r.direction).toBe('BUY');
  });

  it('detects Gold sell now', () => {
    const r = parseMessageText(FIXTURE_MESSAGES.goldSellNow, ctx());
    expect(r.isSignal && r.direction).toBe('SELL');
  });

  it('detects GOLD BUY without now', () => {
    const r = parseMessageText(FIXTURE_MESSAGES.goldBuyCaps, ctx());
    expect(r.isSignal && r.direction).toBe('BUY');
  });

  it('handles emoji wrapper', () => {
    const r = parseMessageText(FIXTURE_MESSAGES.emojiWrapped, ctx());
    expect(r.isSignal && r.direction).toBe('BUY');
  });

  it('rejects past tense bought', () => {
    const r = parseMessageText(FIXTURE_MESSAGES.pastTense, ctx());
    expect(r.isSignal).toBe(false);
  });

  it('rejects buy and sell in same message', () => {
    const r = parseMessageText(FIXTURE_MESSAGES.ambiguous, ctx());
    expect(r.isSignal).toBe(false);
    if (!r.isSignal) expect(r.reason).toBe('ambiguous_direction');
  });

  it('rejects BTC in v1', () => {
    const r = parseMessageText(FIXTURE_MESSAGES.btc, ctx());
    expect(r.isSignal).toBe(false);
  });

  it('rejects empty text', () => {
    expect(parseMessageText('', ctx()).isSignal).toBe(false);
    expect(parseMessageText(undefined, ctx()).isSignal).toBe(false);
  });

  it('maps XAU/USD', () => {
    const r = parseMessageText(FIXTURE_MESSAGES.xauUsd, ctx());
    expect(r.isSignal && r.symbol).toBe('XAU');
  });
});
