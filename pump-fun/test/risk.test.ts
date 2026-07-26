import { describe, it, expect } from 'vitest';
import { RiskManager } from '../src/risk/manager.ts';
import { TypedBus } from '../src/core/bus.ts';
import { openDb } from '../src/persistence/db.ts';
import { Repositories } from '../src/persistence/repositories.ts';
import { ConfigSchema } from '../src/config/schema.ts';
import { LAMPORTS_PER_SOL } from '../src/core/constants.ts';
import type { Position } from '../src/core/types.ts';

function harness(overrides: Record<string, unknown> = {}, balanceLamports?: bigint) {
  const bus = new TypedBus();
  const repos = new Repositories(openDb({ path: ':memory:', memory: true }));
  const config = ConfigSchema.parse({ mode: 'paper', ...overrides });
  let t = Date.UTC(2026, 6, 8, 12, 0, 0); // 2026-07-08T12:00:00Z
  const breakers: Array<{ type: string; tripped: boolean }> = [];
  bus.on('breaker', (b) => breakers.push({ type: b.type, tripped: b.tripped }));
  const risk = new RiskManager({
    config,
    bus,
    repos,
    now: () => t,
    ...(balanceLamports !== undefined ? { getWalletBalanceLamports: async () => balanceLamports } : {}),
  });
  risk.start();
  const closed = (pnlSol: number, trigger?: Position['exitTrigger']) =>
    bus.emit('positionUpdate', { mint: 'm', state: 'CLOSED', sizeSol: 0.25, pnlSol, ...(trigger ? { exitTrigger: trigger } : {}) });
  return { bus, repos, risk, closed, breakers, advance: (ms: number) => (t += ms), setTime: (ms: number) => (t = ms) };
}

describe('RiskManager breakers', () => {
  it('trips DAILY_LOSS and clears at the UTC boundary', () => {
    const h = harness({ risk: { dailyLossLimitSol: 1.5 } });
    expect(h.risk.canEnter().ok).toBe(true);
    h.closed(-1.6);
    expect(h.risk.canEnter()).toMatchObject({ ok: false, reason: 'DAILY_LOSS' });
    expect(h.breakers).toContainEqual({ type: 'DAILY_LOSS', tripped: true });
    h.setTime(Date.UTC(2026, 6, 9, 0, 30, 0)); // next UTC day
    expect(h.risk.canEnter().ok).toBe(true);
  });

  /**
   * The whole point of keeping the twin in its own table: a bad simulated run
   * must never be able to halt real trading. Seeded via the repository (not the
   * bus) because rehydration reads straight from the DB on start.
   */
  it('ignores dry-run twin losses entirely — they cannot trip a breaker', () => {
    const h = harness({ risk: { consecutiveLossHalt: 2, dailyLossLimitSol: 0.05 } });
    for (let i = 0; i < 5; i++) {
      h.repos.upsertDryRunPosition({
        mint: `dry${i}`,
        state: 'CLOSED',
        liveStatus: 'blocked_concurrent',
        sizeSol: 0.25,
        entryPrice: 1,
        exitPrice: 0.1,
        exitReason: 'EMERGENCY_EXIT',
        openedAt: Date.UTC(2026, 6, 8, 11, 0, 0),
        closedAt: Date.UTC(2026, 6, 8, 11, 30, 0),
        netPnlSol: -5,
      });
    }
    // A fresh manager rehydrates from the DB — the path that would leak.
    const bus2 = new TypedBus();
    const breakers: string[] = [];
    bus2.on('breaker', (b) => breakers.push(b.type));
    const risk2 = new RiskManager({
      config: ConfigSchema.parse({ mode: 'paper', risk: { consecutiveLossHalt: 2, dailyLossLimitSol: 0.05 } }),
      bus: bus2,
      repos: h.repos,
      now: () => Date.UTC(2026, 6, 8, 12, 0, 0),
    });
    risk2.start();

    expect(risk2.canEnter().ok).toBe(true);
    expect(breakers).toHaveLength(0);
    risk2.stop();
  });

  it('trips CONSECUTIVE_LOSSES for the halt window, then clears', () => {
    const h = harness({ risk: { consecutiveLossHalt: 4, consecutiveLossHaltMinutes: 120, dailyLossLimitSol: 100 } });
    for (let i = 0; i < 4; i++) h.closed(-0.01);
    expect(h.risk.canEnter()).toMatchObject({ ok: false, reason: 'CONSECUTIVE_LOSSES' });
    h.advance(121 * 60_000);
    expect(h.risk.canEnter().ok).toBe(true);
  });

  it('a win resets the consecutive-loss streak', () => {
    const h = harness({ risk: { consecutiveLossHalt: 3, dailyLossLimitSol: 100 } });
    h.closed(-0.01);
    h.closed(-0.01);
    h.closed(+0.5); // win resets
    h.closed(-0.01);
    expect(h.risk.canEnter().ok).toBe(true);
  });

  it('a win clears an active consecutive-loss halt', () => {
    const h = harness({ risk: { consecutiveLossHalt: 2, consecutiveLossHaltMinutes: 120, dailyLossLimitSol: 100 } });
    h.closed(-0.01);
    h.closed(-0.01);
    expect(h.risk.canEnter()).toMatchObject({ ok: false, reason: 'CONSECUTIVE_LOSSES' });
    h.closed(+0.5);
    expect(h.risk.canEnter().ok).toBe(true);
  });

  it('uses the configurable dry-run consecutive-loss halt window', () => {
    const h = harness({
      mode: 'dry-run',
      risk: {
        consecutiveLossHalt: 2,
        consecutiveLossHaltMinutes: 120,
        dryRunConsecutiveLossHaltMinutes: 10,
        dailyLossLimitSol: 100,
      },
    });
    h.closed(-0.01);
    h.closed(-0.01);
    expect(h.risk.canEnter()).toMatchObject({ ok: false, reason: 'CONSECUTIVE_LOSSES' });
    h.advance(11 * 60_000);
    expect(h.risk.canEnter().ok).toBe(true);
  });

  it('trips EMERGENCY_EXITS on the 24h count', () => {
    const h = harness({ risk: { emergencyExitCount24h: 2, dailyLossLimitSol: 100 } });
    h.closed(-0.01, 'EMERGENCY_EXIT');
    h.closed(-0.01, 'EMERGENCY_EXIT');
    expect(h.risk.canEnter()).toMatchObject({ ok: false, reason: 'EMERGENCY_EXITS' });
  });

  it('trips WALLET_FLOOR from the cached balance', async () => {
    const h = harness({ wallet: { balanceFloorSol: 0.1 }, entry: { baseSizeSol: 0.25 } }, BigInt(0.3 * LAMPORTS_PER_SOL));
    await h.risk.refreshWalletBalance();
    expect(h.risk.canEnter()).toMatchObject({ ok: false, reason: 'WALLET_FLOOR' }); // 0.3 < 0.1+0.25
  });

  /**
   * Previously a balance that was never fetched left the cache null and the
   * floor check simply did not run — so a rate-limited getBalance silently
   * disabled a real safety breaker and let live entries through unchecked.
   * Fail CLOSED: no verifiable balance means no entry.
   */
  it('trips WALLET_FLOOR when the balance has never been readable', () => {
    const bus = new TypedBus();
    const repos = new Repositories(openDb({ path: ':memory:', memory: true }));
    const risk = new RiskManager({
      config: ConfigSchema.parse({ mode: 'paper' }),
      bus,
      repos,
      now: () => Date.UTC(2026, 6, 8, 12, 0, 0),
      getWalletBalanceLamports: async () => {
        throw new Error('getBalance HTTP 429');
      },
    });
    risk.start();
    expect(risk.canEnter()).toMatchObject({ ok: false, reason: 'WALLET_FLOOR' });
    risk.stop();
  });

  it('trips WALLET_FLOOR when the last good balance has gone stale', async () => {
    const bus = new TypedBus();
    const repos = new Repositories(openDb({ path: ':memory:', memory: true }));
    let t = Date.UTC(2026, 6, 8, 12, 0, 0);
    let healthy = true;
    const risk = new RiskManager({
      config: ConfigSchema.parse({ mode: 'paper', wallet: { balanceFloorSol: 0.1 }, entry: { baseSizeSol: 0.02 } }),
      bus,
      repos,
      now: () => t,
      getWalletBalanceLamports: async () => {
        if (!healthy) throw new Error('getBalance HTTP 429');
        return BigInt(5 * LAMPORTS_PER_SOL);
      },
    });
    risk.start();

    await risk.refreshWalletBalance();
    expect(risk.canEnter().ok).toBe(true);

    // RPC starts failing; the cached balance keeps working until it ages out.
    healthy = false;
    t += 60_000;
    await risk.refreshWalletBalance();
    expect(risk.canEnter().ok).toBe(true);

    t += 90_000; // now past the 120s staleness window
    await risk.refreshWalletBalance();
    expect(risk.canEnter()).toMatchObject({ ok: false, reason: 'WALLET_FLOOR' });

    // Recovers cleanly once the RPC answers again.
    healthy = true;
    await risk.refreshWalletBalance();
    expect(risk.canEnter().ok).toBe(true);
    risk.stop();
  });

  it('does not trip WALLET_FLOOR in paper mode, where there is no wallet', () => {
    // No balance provider => nothing to verify => the breaker must stay quiet.
    const h = harness();
    expect(h.risk.canEnter().ok).toBe(true);
  });

  it('gates on stream-down and clears on recovery', () => {
    const h = harness();
    h.bus.emit('streamHealth', { source: 'detector', healthy: false });
    expect(h.risk.canEnter()).toMatchObject({ ok: false, reason: 'STREAM_DOWN' });
    h.bus.emit('streamHealth', { source: 'detector', healthy: true });
    expect(h.risk.canEnter().ok).toBe(true);
  });

  it('latches the kill switch and never auto-clears', () => {
    const h = harness();
    h.bus.emit('killSwitch', { source: 'telegram' });
    expect(h.risk.killed).toBe(true);
    expect(h.risk.canEnter()).toMatchObject({ ok: false, reason: 'KILL_SWITCH' });
    h.advance(10 * 86_400_000); // 10 days later — still killed
    expect(h.risk.canEnter().ok).toBe(false);
  });

  it('persists breaker events and rehydrates a tripped daily loss from the DB', () => {
    const bus = new TypedBus();
    const repos = new Repositories(openDb({ path: ':memory:', memory: true }));
    const config = ConfigSchema.parse({ mode: 'paper', risk: { dailyLossLimitSol: 1 } });
    const day = new Date(Date.UTC(2026, 6, 8, 6, 0, 0)).toISOString().slice(0, 10);
    // Pre-seed a big loss earlier today.
    repos.upsertPosition({ mint: 'x', state: 'CLOSED', sizeSol: 0.25, pnlSol: -1.2, closedAt: Date.UTC(2026, 6, 8, 6, 0, 0) });
    const risk = new RiskManager({ config, bus, repos, now: () => Date.UTC(2026, 6, 8, 12, 0, 0) });
    risk.start(); // rehydrates dailyRealizedPnlSol = -1.2
    expect(risk.canEnter()).toMatchObject({ ok: false, reason: 'DAILY_LOSS' });
    const rows = (repos as unknown as { db: { prepare: (s: string) => { get: () => { n: number } } } }).db
      .prepare("SELECT COUNT(*) AS n FROM breaker_events WHERE type='DAILY_LOSS' AND tripped=1")
      .get();
    expect(rows.n).toBeGreaterThanOrEqual(1);
    void day;
  });

  it('does not restart an elapsed consecutive-loss halt on boot', () => {
    const bus = new TypedBus();
    const repos = new Repositories(openDb({ path: ':memory:', memory: true }));
    const config = ConfigSchema.parse({
      mode: 'live',
      rpc: { primaryHttp: 'https://rpc.example' },
      risk: { consecutiveLossHalt: 2, consecutiveLossHaltMinutes: 120, dailyLossLimitSol: 100 },
    });
    repos.upsertPosition({
      mint: 'old-a',
      state: 'CLOSED',
      sizeSol: 0.03,
      pnlSol: -0.001,
      closedAt: Date.UTC(2026, 6, 8, 9, 0, 0),
    });
    repos.upsertPosition({
      mint: 'old-b',
      state: 'CLOSED',
      sizeSol: 0.03,
      pnlSol: -0.001,
      closedAt: Date.UTC(2026, 6, 8, 9, 30, 0),
    });

    const risk = new RiskManager({ config, bus, repos, now: () => Date.UTC(2026, 6, 8, 12, 0, 0) });
    risk.start();

    expect(risk.canEnter().ok).toBe(true);
  });
});
