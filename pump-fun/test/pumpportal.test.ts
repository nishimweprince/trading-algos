import { describe, it, expect, vi, afterEach } from 'vitest';
import { PumpPortalFeed } from '../src/detector/pumpportal.ts';
import type { FeedGraduation } from '../src/core/types.ts';

const TOKEN = '64W4CqYgGzco1Rm5cgHepUWPvqPWWTJ6NRRWwLVGpump';

class FakeWs {
  static instance: FakeWs | null = null;
  handlers: Record<string, Array<(ev: unknown) => void>> = {};
  sent: string[] = [];
  readyState = 1;
  url: string;
  constructor(url: string) {
    this.url = url;
    FakeWs.instance = this;
  }
  addEventListener(type: string, cb: (ev: unknown) => void) {
    (this.handlers[type] ??= []).push(cb);
  }
  send(d: string) {
    this.sent.push(d);
  }
  close() {
    this.fire('close', { code: 1000 });
  }
  fire(type: string, ev: unknown) {
    for (const h of this.handlers[type] ?? []) h(ev);
  }
}

describe('PumpPortalFeed', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.useRealTimers();
    FakeWs.instance = null;
  });

  function make() {
    vi.stubGlobal('WebSocket', FakeWs);
    const grads: FeedGraduation[] = [];
    const health: Array<{ healthy: boolean; detail?: string }> = [];
    const activity: string[] = [];
    const feed = new PumpPortalFeed({ url: 'wss://pumpportal.fun/api/data', reconnectBaseMs: 10, reconnectMaxMs: 100 });
    feed.onGraduation((g) => grads.push(g));
    feed.onHealth((healthy, detail) => health.push(detail !== undefined ? { healthy, detail } : { healthy }));
    feed.onActivity((a) => activity.push(a.kind));
    feed.start();
    return { feed, grads, health, activity, ws: FakeWs.instance! };
  }

  it('has silence liveness and turns a migration payload into a graduation', () => {
    const { feed, ws, grads, activity } = make();
    expect(feed.liveness).toBe('silence');
    ws.fire('open', {});
    expect(ws.sent[0]).toContain('subscribeMigration');
    ws.fire('message', { data: JSON.stringify({ message: 'Successfully subscribed to migration events.' }) });
    expect(grads).toHaveLength(0);
    expect(activity).toEqual(['data']); // acks count as activity
    ws.fire('message', { data: JSON.stringify({ mint: TOKEN, signature: 'PPSIG', pool: 'pump-amm' }) });
    expect(grads).toHaveLength(1);
    expect(grads[0]).toMatchObject({ mint: TOKEN, feedSource: 'pumpportal', signature: 'PPSIG', venue: 'pumpswap' });
    expect(activity).toEqual(['data', 'data']);
  });

  it('reconnect() abandons the socket, opens a new one after backoff, ignores stale close', () => {
    vi.useFakeTimers();
    const { feed, ws, health, grads } = make();
    ws.fire('open', {});
    const first = ws;
    feed.reconnect('coverage: missed 3');
    expect(health.filter((h) => !h.healthy)).toEqual([{ healthy: false, detail: 'coverage: missed 3' }]);
    feed.reconnect('again');
    expect(health.filter((h) => !h.healthy)).toHaveLength(1);
    vi.advanceTimersByTime(20);
    const second = FakeWs.instance!;
    expect(second).not.toBe(first);
    first.fire('close', { code: 1006 });
    first.fire('message', { data: JSON.stringify({ mint: TOKEN }) });
    expect(grads).toHaveLength(0);
    expect(health.filter((h) => !h.healthy)).toHaveLength(1);
    second.fire('open', {});
    expect(health.at(-1)).toEqual({ healthy: true });
  });

  it('reconnect() is a no-op when nothing is connected', () => {
    vi.useFakeTimers();
    vi.stubGlobal('WebSocket', FakeWs);
    const feed = new PumpPortalFeed({ url: 'wss://x', reconnectBaseMs: 10, reconnectMaxMs: 100 });
    const health: boolean[] = [];
    feed.onHealth((h) => health.push(h));
    feed.reconnect('nothing');
    expect(health).toEqual([]);
  });
});
