import { describe, it, expect, vi } from 'vitest';
import { TypedBus } from '../src/core/bus.ts';

describe('TypedBus', () => {
  it('delivers typed payloads to subscribers', () => {
    const bus = new TypedBus();
    const seen: string[] = [];
    bus.on('alert', (a) => seen.push(a.message));
    bus.emit('alert', { level: 'info', message: 'hello' });
    expect(seen).toEqual(['hello']);
  });

  it('unsubscribe stops delivery', () => {
    const bus = new TypedBus();
    const handler = vi.fn();
    const off = bus.on('graduation', handler);
    off();
    bus.emit('graduation', {
      mint: 'm',
      venue: 'pumpswap',
      poolAddress: 'p',
      slot: 1,
      feedSource: 'grpc',
      receivedAtNs: 0n,
    });
    expect(handler).not.toHaveBeenCalled();
  });

  it('once fires exactly once', () => {
    const bus = new TypedBus();
    const handler = vi.fn();
    bus.once('killSwitch', handler);
    bus.emit('killSwitch', { source: 'internal' });
    bus.emit('killSwitch', { source: 'internal' });
    expect(handler).toHaveBeenCalledTimes(1);
  });
});
