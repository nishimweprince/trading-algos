import { describe, it, expect, vi, afterEach } from 'vitest';
import { PumpPortalFeed, classifyPayload } from '../src/detector/pumpportal.ts';
import { Detector } from '../src/detector/index.ts';
import type { DetectionFeed, FeedActivity, FeedLiveness } from '../src/detector/feed.ts';
import { ConfigSchema } from '../src/config/schema.ts';
import { TypedBus } from '../src/core/bus.ts';
import { openDb } from '../src/persistence/db.ts';
import { Repositories } from '../src/persistence/repositories.ts';
import { SlotClock } from '../src/core/slotClock.ts';
import type { FeedGraduation, FeedLaunch, FeedSource, GraduationEvent } from '../src/core/types.ts';

const TOKEN = '64W4CqYgGzco1Rm5cgHepUWPvqPWWTJ6NRRWwLVGpump';

class FakeWs {
  static instance: FakeWs | null = null;
  handlers: Record<string, Array<(ev: unknown) => void>> = {};
  sent: string[] = [];
  readyState = 1;
  constructor(url: string) {
    void url;
    FakeWs.instance = this;
  }
  addEventListener(type: string, cb: (ev: unknown) => void) {
    (this.handlers[type] ??= []).push(cb);
  }
  send(d: string) {
    this.sent.push(d);
  }
  close() {}
  fire(type: string, ev: unknown) {
    for (const h of this.handlers[type] ?? []) h(ev);
  }
}

function makeFeed(opts: { newTokenEnabled?: boolean } = {}) {
  vi.stubGlobal('WebSocket', FakeWs);
  const grads: FeedGraduation[] = [];
  const launches: FeedLaunch[] = [];
  const feed = new PumpPortalFeed({
    url: 'wss://pumpportal.fun/api/data',
    reconnectBaseMs: 10,
    reconnectMaxMs: 100,
    ...opts,
  });
  feed.onGraduation((g) => grads.push(g));
  feed.onLaunch((l) => launches.push(l));
  feed.start();
  return { feed, grads, launches, ws: FakeWs.instance! };
}

describe('classifyPayload', () => {
  it('routes by txType: create → launch, migrate → graduation, trade frames → skip', () => {
    expect(classifyPayload({ txType: 'create', mint: TOKEN })).toBe('launch');
    expect(classifyPayload({ txType: 'Migrate' })).toBe('graduation');
    expect(classifyPayload({ txType: 'migrateV2' })).toBe('graduation');
    expect(classifyPayload({ txType: 'buy', mint: TOKEN })).toBe('skip');
    expect(classifyPayload({ txType: 'sell', mint: TOKEN })).toBe('skip');
  });

  it('without txType only signature+pool stays on the graduation path', () => {
    expect(classifyPayload({ mint: TOKEN, signature: 'S', pool: 'pump-amm' })).toBe('graduation');
    expect(classifyPayload({ mint: TOKEN })).toBe('launch');
    expect(classifyPayload({ mint: TOKEN, signature: 'S' })).toBe('launch');
  });
});

describe('PumpPortalFeed launches', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    FakeWs.instance = null;
  });

  it('subscribes to both streams and routes a creation payload to onLaunch only', () => {
    const { ws, grads, launches } = makeFeed();
    ws.fire('open', {});
    expect(ws.sent[0]).toContain('subscribeMigration');
    expect(ws.sent[1]).toContain('subscribeNewToken');
    ws.fire('message', {
      data: JSON.stringify({ txType: 'create', mint: TOKEN, name: 'Foo', symbol: 'FOO', creator: 'DEV1' }),
    });
    expect(launches).toHaveLength(1);
    expect(launches[0]).toMatchObject({ mint: TOKEN, feedSource: 'pumpportal', name: 'Foo', symbol: 'FOO', creator: 'DEV1' });
    expect(grads).toHaveLength(0);
  });

  it('keeps the verified migration shape on the graduation path', () => {
    const { ws, grads, launches } = makeFeed();
    ws.fire('open', {});
    ws.fire('message', { data: JSON.stringify({ mint: TOKEN, signature: 'PPSIG', pool: 'pump-amm' }) });
    expect(grads).toHaveLength(1);
    expect(launches).toHaveLength(0);
  });

  it('skips trade frames on either path', () => {
    const { ws, grads, launches } = makeFeed();
    ws.fire('open', {});
    ws.fire('message', { data: JSON.stringify({ txType: 'buy', mint: TOKEN }) });
    expect(grads).toHaveLength(0);
    expect(launches).toHaveLength(0);
  });

  it('sends only the migration subscribe when newTokenEnabled is false', () => {
    const { ws } = makeFeed({ newTokenEnabled: false });
    ws.fire('open', {});
    expect(ws.sent).toHaveLength(1);
    expect(ws.sent[0]).toContain('subscribeMigration');
  });
});

class FakeLaunchFeed implements DetectionFeed {
  readonly name: FeedSource = 'pumpportal';
  readonly liveness: FeedLiveness = 'silence';
  private grad: (g: FeedGraduation) => void = () => {};
  private launch: (l: FeedLaunch) => void = () => {};
  private health: (healthy: boolean, detail?: string) => void = () => {};
  private activity: (a: FeedActivity) => void = () => {};
  start() {}
  async stop() {}
  onGraduation(h: (g: FeedGraduation) => void) {
    this.grad = h;
  }
  onLaunch(h: (l: FeedLaunch) => void) {
    this.launch = h;
  }
  onHealth(h: (healthy: boolean, detail?: string) => void) {
    this.health = h;
  }
  onActivity(h: (a: FeedActivity) => void) {
    this.activity = h;
  }
  reconnect(_reason: string) {}
  emitLaunch(mint: string) {
    this.launch({ mint, feedSource: 'pumpportal', receivedAtNs: process.hrtime.bigint() });
  }
  emitGraduation(mint: string) {
    this.grad({ mint, feedSource: 'pumpportal', receivedAtNs: process.hrtime.bigint() });
  }
}

describe('Detector launch path (S0 observe-only)', () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  function setup() {
    vi.useFakeTimers();
    vi.setSystemTime(1_000_000);
    const config = ConfigSchema.parse({ rpc: { primaryHttp: 'http://x' } });
    const bus = new TypedBus();
    const db = openDb({ path: ':memory:', memory: true });
    const repos = new Repositories(db);
    const slotClock = new SlotClock({ now: () => Date.now() });
    const feed = new FakeLaunchFeed();
    const grads: GraduationEvent[] = [];
    bus.on('graduation', (g) => grads.push(g));
    const detector = new Detector({ config, bus, repos, slotClock, feeds: [feed], now: () => Date.now() });
    return { detector, repos, feed, grads, db };
  }

  it('persists a launch once across redeliveries and never screens it', async () => {
    const { detector, repos, feed, grads, db } = setup();
    await detector.start();
    feed.emitLaunch(TOKEN);
    feed.emitLaunch(TOKEN);
    expect(grads).toHaveLength(0);
    const rows = db.prepare('SELECT mint FROM launches').all();
    expect(rows).toHaveLength(1);
    expect(repos.countLaunchesSince('1970-01-01 00:00:00')).toBe(1);
    // No candidate row: launches never enter screening.
    expect(db.prepare('SELECT COUNT(*) AS n FROM candidates').get()).toEqual({ n: 0 });
    await detector.stop();
  });

  it('a later graduation of the same mint still flows through screening', async () => {
    const { detector, feed, grads } = setup();
    await detector.start();
    feed.emitLaunch(TOKEN);
    feed.emitGraduation(TOKEN);
    expect(grads).toHaveLength(1);
    expect(grads[0]!.mint).toBe(TOKEN);
    await detector.stop();
  });
});
