import { describe, it, expect } from 'vitest';
import type { Signal } from '../src/signals/types.js';
import { formatSignal } from '../src/notifications/formatter.js';

function sig(over: Partial<Signal> = {}): Signal {
  return {
    id: 'id',
    symbol: 'XAU',
    direction: 'BUY',
    sourceChannel: '@xauusdsignals',
    messageId: 1,
    messageDate: '2026-05-04T18:42:00.000Z',
    rawText: 'x',
    channelId: '-1001',
    ...over,
  };
}

describe('formatSignal', () => {
  it('stays within 150 chars for realistic input', () => {
    const body = formatSignal(sig(), 'AcmeCo');
    expect(body.length).toBeLessThanOrEqual(150);
    expect(body).toContain('XAU');
    expect(body).toContain('BUY');
  });

  it('truncates very long channel suffix', () => {
    const long = `@${'x'.repeat(120)}`;
    const body = formatSignal(sig({ sourceChannel: long }), 'Short');
    expect(body.length).toBeLessThanOrEqual(150);
    expect(body.endsWith('...') || body.length === 150).toBe(true);
  });
});
