import { describe, it, expect, vi, afterEach } from 'vitest';
import { Detector } from '../src/detector/index.ts';
import type { DetectionFeed, FeedActivity, FeedLiveness } from '../src/detector/feed.ts';
import { ConfigSchema } from '../src/config/schema.ts';
import { TypedBus } from '../src/core/bus.ts';
import { openDb } from '../src/persistence/db.ts';
import { Repositories } from '../src/persistence/repositories.ts';
import { SlotClock } from '../src/core/slotClock.ts';
import type { FeedGraduation, FeedSource, GraduationEvent } from '../src/core/types.ts';

class FakeFeed implements DetectionFeed {
  liveness: FeedLiveness;
  reconnectCalls: string[] = [];
  started = 0;
  private grad: (g: FeedGraduation) => void = () => {};
  private health: (healthy: boolean, detail?: string) => void = () => {};
  private activity: (a: FeedActivity) => void = () => {};
  readonly name: FeedSource;
  constructor(name: FeedSource, liveness: FeedLiveness) {
    this.name = name;
    this.liveness = liveness;
  }
  start() {
    this.started++;
  }
  async stop() {}
  onGraduation(h: (g: FeedGraduation) => void) {
    this.grad = h;
  }
  onHealth(h: (healthy: boolean, detail?: string) => void) {
    this.health = h;
  }
  onActivity(h: (a: FeedActivity) => void) {
    this.activity = h;
  }
  reconnect(reason: string) {
    this.reconnectCalls.push(reason);
  }
  // test drivers
  healthy(v = true) {
    this.health(v, v ? undefined : 'closed');
  }
  tick(slot: number, atMs: number) {
    this.activity({ atMs, kind: 'slot', slot });
  }
  data(atMs: number) {
    this.activity({ atMs, kind: 'data' });
  }
  emit(mint: string, slot?: number) {
    const g: FeedGraduation = { mint, feedSource: this.name, receivedAtNs: process.hrtime.bigint() };
    if (slot !== undefined) g.slot = slot;
    this.grad(g);
  }
}

function setup(overrides: Record<string, unknown> = {}) {
  vi.useFakeTimers();
  vi.setSystemTime(1_000_000);
  const now = () => Date.now();
  const config = ConfigSchema.parse({ rpc: { primaryHttp: 'http://x' }, ...overrides });
  const bus = new TypedBus();
  const repos = new Repositories(openDb({ path: ':memory:', memory: true }));
  const slotClock = new SlotClock({ now });
  const portal = new FakeFeed('pumpportal', 'silence');
  const laser = new FakeFeed('laserstream', 'slot');
  const grads: GraduationEvent[] = [];
  const alerts: Array<{ level: string; message: string }> = [];
  const streamHealth: boolean[] = [];
  bus.on('graduation', (g) => grads.push(g));
  bus.on('alert', (a) => alerts.push({ level: a.level, message: a.message }));
  bus.on('streamHealth', (s) => streamHealth.push(s.healthy));
  const detector = new Detector({ config, bus, repos, slotClock, feeds: [portal, laser], now });
  return { detector, config, repos, slotClock, portal, laser, grads, alerts, streamHealth };
}

const latencyRows = (repos: Repositories, kind: string) =>
  (repos as unknown as { db: { prepare(sql: string): { all(k: string): Array<{ latency_ms: number; feed_source: string }> } } }).db
    .prepare('SELECT latency_ms, feed_source FROM latency_samples WHERE kind = ?')
    .all(kind);

describe('Detector orchestration', () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it('stamps receivedSlot on graduations and records a detection_slots sample', async () => {
    const { detector, laser, grads, repos } = setup();
    await detector.start();
    laser.healthy();
    laser.tick(1000, Date.now());
    laser.emit('MINT_A', 998);
    expect(grads).toHaveLength(1);
    expect(grads[0]).toMatchObject({ mint: 'MINT_A', slot: 998, receivedSlot: 1000 });
    await vi.advanceTimersByTimeAsync(10); // confirmAndPersist (no rpc → confirm skipped)
    const rows = latencyRows(repos, 'detection_slots');
    expect(rows).toEqual([{ latency_ms: 2, feed_source: 'laserstream' }]);
    await detector.stop();
  });

  it('drops a migration older than maxStaleSlots (feed replay)', async () => {
    const { detector, laser, grads } = setup({ detector: { maxStaleSlots: 150 } });
    await detector.start();
    laser.healthy();
    laser.tick(10_000, Date.now());
    laser.emit('OLD', 9_000);
    expect(grads).toHaveLength(0);
    laser.emit('FRESH', 9_990);
    expect(grads).toHaveLength(1);
    await detector.stop();
  });

  it('forces a reconnect on a silent slot feed without tripping STREAM_DOWN while another feed is healthy', async () => {
    const { detector, laser, portal, streamHealth, alerts } = setup();
    await detector.start();
    laser.healthy();
    portal.healthy();
    laser.tick(1, Date.now());
    await vi.advanceTimersByTimeAsync(4_000);
    expect(laser.reconnectCalls).toEqual([]);
    await vi.advanceTimersByTimeAsync(2_000);
    expect(laser.reconnectCalls).toHaveLength(1);
    expect(laser.reconnectCalls[0]).toMatch(/watchdog: silent/);
    expect(portal.reconnectCalls).toEqual([]);
    expect(streamHealth).toEqual([]); // portal still healthy → no outage
    expect(alerts.some((a) => a.message.includes('laserstream silent'))).toBe(true);
    // Suppressed until the feed reports healthy again; then the clock restarts.
    await vi.advanceTimersByTimeAsync(10_000);
    expect(laser.reconnectCalls).toHaveLength(1);
    laser.healthy();
    await vi.advanceTimersByTimeAsync(6_000);
    expect(laser.reconnectCalls).toHaveLength(2);
    await detector.stop();
  });

  it('reconnects PumpPortal after it misses N graduations the on-chain feed saw (rule A)', async () => {
    const { detector, laser, portal } = setup();
    await detector.start();
    laser.healthy();
    portal.healthy();
    for (const m of ['M1', 'M2', 'M3']) {
      laser.tick(5, Date.now());
      laser.emit(m, 5);
      await vi.advanceTimersByTimeAsync(1_000);
    }
    expect(portal.reconnectCalls).toEqual([]);
    await vi.advanceTimersByTimeAsync(25_000); // settle window
    expect(portal.reconnectCalls).toHaveLength(1);
    expect(portal.reconnectCalls[0]).toMatch(/coverage: missed 3/);
    await detector.stop();
  });

  it('a second-feed sighting inside the dedupe window counts as coverage (no reconnect)', async () => {
    const { detector, laser, portal } = setup();
    await detector.start();
    laser.healthy();
    portal.healthy();
    for (const m of ['M1', 'M2', 'M3']) {
      laser.emit(m, 5);
      portal.emit(m); // duplicate — dropped for dispatch, but observed for coverage
      await vi.advanceTimersByTimeAsync(1_000);
    }
    await vi.advanceTimersByTimeAsync(25_000);
    expect(portal.reconnectCalls).toEqual([]);
    await detector.stop();
  });

  it('raises the authority tripwire when only PumpPortal sees migrations while on-chain feeds are healthy (rule B)', async () => {
    const { detector, laser, portal, alerts } = setup();
    await detector.start();
    laser.healthy();
    portal.healthy();
    for (const m of ['P1', 'P2', 'P3']) {
      laser.tick(7, Date.now()); // keeps the slot feed alive
      portal.emit(m);
      await vi.advanceTimersByTimeAsync(1_000);
      laser.tick(8, Date.now());
    }
    // keep laser ticking through the settle window so it is not reconnected
    for (let i = 0; i < 25; i++) {
      laser.tick(10 + i, Date.now());
      await vi.advanceTimersByTimeAsync(1_000);
    }
    const trip = alerts.filter((a) => a.level === 'error' && a.message.includes('migrationAuthority'));
    expect(trip).toHaveLength(1);
    expect(laser.reconnectCalls).toEqual([]);
    await detector.stop();
  });

  it('stop() clears the watchdog interval', async () => {
    const { detector, laser } = setup();
    await detector.start();
    laser.healthy();
    await detector.stop();
    await vi.advanceTimersByTimeAsync(30_000);
    expect(laser.reconnectCalls).toEqual([]);
  });
});
