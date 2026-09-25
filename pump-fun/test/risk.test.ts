import { writeFileSync, existsSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { describe, it, expect, vi } from 'vitest';
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

  it('does not trip DAILY_LOSS on a flat day when the loss limit is zero', async () => {
    // Empty wallet → the % of-wallet cap is 0 → computed limit is 0. Zero PnL
    // must not satisfy `pnl <= -0`; WALLET_FLOOR already gates entries.
    const h = harness(
      { mode: 'live', rpc: { primaryHttp: 'http://x' }, risk: { dailyLossLimitSol: 0.02, dailyLossLimitWalletPct: 5 } },
      0n,
    );
    await h.risk.refreshWalletBalance(); // boot primes the cache: empty wallet reads 0n
    expect(h.breakers).not.toContainEqual({ type: 'DAILY_LOSS', tripped: true });
    expect(h.risk.canEnter()).toMatchObject({ ok: false, reason: 'WALLET_FLOOR' });
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

  /**
   * Block-lift regression (2026-09-10): the live 24h emergency counter sat at
   * 50/50 and tripped EMERGENCY_EXITS, so the limit was raised to 200. A
   * restart rehydrates the same 50 rows from the DB — entries must be open
   * again under the raised limit.
   */
  it('a raised 200 limit lifts the block with 50 recent emergency exits', () => {
    const bus = new TypedBus();
    const db = openDb({ path: ':memory:', memory: true });
    const repos = new Repositories(db);
    const nowMs = Date.UTC(2026, 6, 8, 12, 0, 0);
    for (let i = 0; i < 50; i++) {
      db.prepare(
        `INSERT INTO positions (mint, size_sol, state, exit_reason, pnl_sol, closed_at) VALUES (?, 0.25, 'CLOSED', 'EMERGENCY_EXIT', -0.01, ?)`,
      ).run(`m${i}`, new Date(nowMs - i * 60_000).toISOString());
    }
    const risk = new RiskManager({
      // consecutiveLossHalt raised so only the EMERGENCY_EXITS gate is under test.
      config: ConfigSchema.parse({
        mode: 'paper',
        risk: { emergencyExitCount24h: 200, consecutiveLossHalt: 1000, dailyLossLimitSol: 100 },
      }),
      bus,
      repos,
      now: () => nowMs,
    });
    risk.start();
    expect(risk.getSnapshot().emergencies24h).toBe(50);
    expect(risk.canEnter().ok).toBe(true);
    risk.stop();
    db.close();
  });

  /**
   * Block-lift regression (2026-09-14): the live 24h emergency counter hit
   * 200/200 and tripped EMERGENCY_EXITS, so the limit was raised to 1000.
   * 200 recent emergency exits must leave entries open under the new limit.
   */
  it('a raised 1000 limit lifts the block with 200 recent emergency exits', () => {
    const bus = new TypedBus();
    const db = openDb({ path: ':memory:', memory: true });
    const repos = new Repositories(db);
    const nowMs = Date.UTC(2026, 6, 8, 12, 0, 0);
    for (let i = 0; i < 200; i++) {
      db.prepare(
        `INSERT INTO positions (mint, size_sol, state, exit_reason, pnl_sol, closed_at) VALUES (?, 0.25, 'CLOSED', 'EMERGENCY_EXIT', -0.01, ?)`,
      ).run(`m${i}`, new Date(nowMs - i * 60_000).toISOString());
    }
    const risk = new RiskManager({
      // consecutiveLossHalt raised so only the EMERGENCY_EXITS gate is under test.
      config: ConfigSchema.parse({
        mode: 'paper',
        risk: { emergencyExitCount24h: 1000, consecutiveLossHalt: 1000, dailyLossLimitSol: 100 },
      }),
      bus,
      repos,
      now: () => nowMs,
    });
    risk.start();
    expect(risk.getSnapshot().emergencies24h).toBe(200);
    expect(risk.canEnter().ok).toBe(true);
    risk.stop();
    db.close();
  });

  it('trips WALLET_FLOOR from the cached balance', async () => {
    const h = harness(
      { wallet: { balanceFloorSol: 0.1 }, entry: { minAbsoluteSol: 0.25 } },
      BigInt(0.3 * LAMPORTS_PER_SOL),
    );
    await h.risk.refreshWalletBalance();
    expect(h.risk.canEnter()).toMatchObject({ ok: false, reason: 'WALLET_FLOOR' }); // 0.3 < 0.1+0.25
  });

  it('gates entries on the dust floor and names it in the message', async () => {
    const h = harness(
      { wallet: { balanceFloorSol: 0.1 }, entry: { minAbsoluteSol: 0.19 } },
      BigInt(0.2 * LAMPORTS_PER_SOL),
    );
    await h.risk.refreshWalletBalance();
    const decision = h.risk.canEnter();
    expect(decision).toMatchObject({ ok: false, reason: 'WALLET_FLOOR' });
    expect(decision.detail).toContain('0.200');
    expect(decision.detail).toContain('0.290');
    expect(decision.detail).toContain('min absolute size');
    expect(h.risk.requiredBalanceSol()).toBeCloseTo(0.29, 9);
  });

  it('allows a percent-sized entry when the wallet clears floor + minAbsoluteSol', async () => {
    const h = harness(
      { wallet: { balanceFloorSol: 0.1 }, entry: { minAbsoluteSol: 0.01, minSizeWalletPct: 5, baseSizeWalletPct: 8, maxSizeWalletPct: 10 } },
      BigInt(0.328 * LAMPORTS_PER_SOL),
    );
    await h.risk.refreshWalletBalance();
    expect(h.risk.canEnter().ok).toBe(true);
    expect(h.risk.requiredBalanceSol()).toBeCloseTo(0.11, 9);
  });

  it('sizes and gates from the in-memory cache without another RPC read', async () => {
    let fetches = 0;
    const bus = new TypedBus();
    const repos = new Repositories(openDb({ path: ':memory:', memory: true }));
    const risk = new RiskManager({
      config: ConfigSchema.parse({
        mode: 'live',
        rpc: { primaryHttp: 'http://x' },
        wallet: { balanceFloorSol: 0.1 },
        entry: { minAbsoluteSol: 0.01, minSizeWalletPct: 5, baseSizeWalletPct: 8, maxSizeWalletPct: 10 },
      }),
      bus,
      repos,
      now: () => Date.UTC(2026, 6, 8, 12, 0, 0),
      getWalletBalanceLamports: async () => {
        fetches += 1;
        return BigInt(0.328 * LAMPORTS_PER_SOL);
      },
    });
    risk.start();
    await risk.refreshWalletBalance();
    expect(fetches).toBe(1);
    expect(risk.cachedBalanceLamports()).toBe(BigInt(0.328 * LAMPORTS_PER_SOL));
    expect(risk.getSnapshot().walletBalanceSol).toBeCloseTo(0.328, 9);
    expect(risk.canEnter().ok).toBe(true);

    risk.reserveSol(0.026);
    expect(fetches).toBe(1);
    expect(Number(risk.cachedBalanceLamports()) / LAMPORTS_PER_SOL).toBeCloseTo(0.302, 3);

    risk.releaseSol(0.026);
    expect(Number(risk.cachedBalanceLamports()) / LAMPORTS_PER_SOL).toBeCloseTo(0.328, 3);

    risk.reserveSol(0.026);
    risk.applyBalanceDeltaSol(0.026 + 0.004); // confirmed exit: size back + pnl
    expect(Number(risk.cachedBalanceLamports()) / LAMPORTS_PER_SOL).toBeCloseTo(0.332, 3);
    expect(fetches).toBe(1);
    risk.stop();
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
      config: ConfigSchema.parse({ mode: 'paper', wallet: { balanceFloorSol: 0.1 }, entry: { minAbsoluteSol: 0.02 } }),
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

  /**
   * Regression: on a quiet chain (devnet) minutes can pass with no graduations,
   * so no screening ever refreshes the balance. The periodic timer must keep
   * the cache fresh on its own — otherwise WALLET_FLOOR trips fail-closed on a
   * funded wallet with a healthy RPC.
   */
  it('keeps the wallet balance fresh on a quiet chain with no screenings', async () => {
    vi.useFakeTimers();
    try {
      const bus = new TypedBus();
      const repos = new Repositories(openDb({ path: ':memory:', memory: true }));
      let t = Date.UTC(2026, 6, 8, 12, 0, 0);
      let calls = 0;
      const risk = new RiskManager({
        config: ConfigSchema.parse({ mode: 'paper', wallet: { balanceFloorSol: 0.1 }, entry: { baseSizeWalletPct: 5 } }),
        bus,
        repos,
        now: () => t,
        getWalletBalanceLamports: async () => {
          calls += 1;
          return BigInt(5 * LAMPORTS_PER_SOL);
        },
      });
      risk.start();
      await risk.refreshWalletBalance(); // boot prime
      expect(calls).toBe(1);

      // 3 minutes pass with zero screenings — past the 120s staleness window.
      t += 180_000;
      await vi.advanceTimersByTimeAsync(180_000);
      expect(calls).toBeGreaterThanOrEqual(3);
      expect(risk.canEnter().ok).toBe(true);
      risk.stop();
    } finally {
      vi.useRealTimers();
    }
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

describe('RiskManager operator day-reset', () => {
  const DAY = Date.UTC(2026, 6, 8, 12, 0, 0); // 2026-07-08T12:00:00Z

  function resetHarness() {
    const bus = new TypedBus();
    const repos = new Repositories(openDb({ path: ':memory:', memory: true }));
    const config = ConfigSchema.parse({
      mode: 'paper',
      risk: { dailyLossLimitSol: 1, consecutiveLossHalt: 5, consecutiveLossHaltMinutes: 10 },
    });
    const alerts: Array<{ message: string; telegram: boolean }> = [];
    bus.on('alert', (a) => alerts.push({ message: a.message, telegram: a.telegram === true }));
    return { bus, repos, config, alerts };
  }

  it('an operator reset clears a tripped daily loss but keeps the ledger rows', () => {
    const h = resetHarness();
    h.repos.upsertPosition({ mint: 'x', state: 'CLOSED', sizeSol: 0.25, pnlSol: -1.2, closedAt: Date.UTC(2026, 6, 8, 6, 0, 0) });
    const before = new RiskManager({ config: h.config, bus: h.bus, repos: h.repos, now: () => DAY });
    before.start();
    expect(before.canEnter()).toMatchObject({ ok: false, reason: 'DAILY_LOSS' });
    before.stop();

    h.repos.recordRiskDayReset('test reset', DAY);
    const after = new RiskManager({ config: h.config, bus: h.bus, repos: h.repos, now: () => DAY });
    after.start();
    expect(after.canEnter().ok).toBe(true);
    after.stop();
    // Pre-reset losses stay in the ledger.
    expect(h.repos.sumRealizedPnlSince('2026-07-08T00:00:00Z')).toBeCloseTo(-1.2, 10);
  });

  it('a stale (pre-midnight) reset marker is ignored', () => {
    const h = resetHarness();
    h.repos.upsertPosition({ mint: 'x', state: 'CLOSED', sizeSol: 0.25, pnlSol: -1.2, closedAt: Date.UTC(2026, 6, 8, 6, 0, 0) });
    h.repos.recordRiskDayReset('yesterday', Date.UTC(2026, 6, 7, 23, 0, 0));
    const risk = new RiskManager({ config: h.config, bus: h.bus, repos: h.repos, now: () => DAY });
    risk.start();
    expect(risk.canEnter()).toMatchObject({ ok: false, reason: 'DAILY_LOSS' });
    risk.stop();
  });

  it('an operator reset clears the consecutive-loss streak; later losses count fresh', () => {
    const h = resetHarness();
    for (let i = 0; i < 5; i++) {
      h.repos.upsertPosition({
        mint: `loss${i}`,
        state: 'CLOSED',
        sizeSol: 0.03,
        pnlSol: -0.001,
        closedAt: Date.UTC(2026, 6, 8, 9, 0, 0) + i * 60_000,
      });
    }
    const before = new RiskManager({ config: h.config, bus: h.bus, repos: h.repos, now: () => Date.UTC(2026, 6, 8, 9, 10, 0) });
    before.start();
    expect(before.canEnter()).toMatchObject({ ok: false, reason: 'CONSECUTIVE_LOSSES' });
    before.stop();

    h.repos.recordRiskDayReset('test reset', Date.UTC(2026, 6, 8, 9, 30, 0));
    const after = new RiskManager({
      config: h.config,
      bus: h.bus,
      repos: h.repos,
      now: () => Date.UTC(2026, 6, 8, 10, 0, 0),
    });
    after.start();
    expect(after.canEnter().ok).toBe(true);
    after.stop();
  });

  it('a RESET_DAY sentinel is consumed one-shot with audit + telegram alert', () => {
    const h = resetHarness();
    h.repos.upsertPosition({ mint: 'x', state: 'CLOSED', sizeSol: 0.25, pnlSol: -1.2, closedAt: Date.UTC(2026, 6, 8, 6, 0, 0) });
    const sentinel = join(tmpdir(), `RESET_DAY-test-${Date.now()}`);
    writeFileSync(sentinel, 'operator reset');
    const risk = new RiskManager({
      config: h.config,
      bus: h.bus,
      repos: h.repos,
      now: () => DAY,
      dayResetSentinelPath: sentinel,
    });
    risk.start();
    expect(existsSync(sentinel)).toBe(false);
    expect(h.repos.lastRiskDayResetAt()).not.toBeNull();
    expect(risk.canEnter().ok).toBe(true);
    expect(h.alerts.some((a) => a.telegram === true && a.message.includes('day-risk reset'))).toBe(true);
    const audit = (h.repos as unknown as { db: { prepare: (s: string) => { get: () => { n: number } } } }).db
      .prepare(`SELECT COUNT(*) AS n FROM operator_events WHERE category='risk'`)
      .get();
    expect(audit.n).toBe(1);
    risk.stop();
    // Second boot without the sentinel does not record another reset.
    const risk2 = new RiskManager({
      config: h.config,
      bus: h.bus,
      repos: h.repos,
      now: () => DAY,
      dayResetSentinelPath: sentinel,
    });
    risk2.start();
    const resets = (h.repos as unknown as { db: { prepare: (s: string) => { get: () => { n: number } } } }).db
      .prepare(`SELECT COUNT(*) AS n FROM risk_day_resets`)
      .get();
    expect(resets.n).toBe(1);
    risk2.stop();
  });

  it('dry-run assumes every wallet starts with 1 SOL and never trips WALLET_FLOOR', async () => {
    // Unfunded/ephemeral wallet reads 0 — without the virtual ledger this
    // latches WALLET_FLOOR and halts all dry-run trading at boot.
    const h = harness({ mode: 'dry-run' }, 0n);
    await h.risk.refreshWalletBalance(); // boot prime must not overwrite the virtual 1 SOL
    expect(h.risk.getSnapshot().walletBalanceSol).toBeCloseTo(1, 9);
    expect(h.risk.canEnter().ok).toBe(true);
    expect(h.breakers).not.toContainEqual({ type: 'WALLET_FLOOR', tripped: true });
  });

  it('dry-run keeps the virtual 1 SOL ledger when the RPC balance is unreadable', async () => {
    const bus = new TypedBus();
    const repos = new Repositories(openDb({ path: ':memory:', memory: true }));
    const risk = new RiskManager({
      config: ConfigSchema.parse({ mode: 'dry-run' }),
      bus,
      repos,
      now: () => Date.UTC(2026, 6, 8, 12, 0, 0),
      getWalletBalanceLamports: async () => {
        throw new Error('getBalance HTTP 429');
      },
    });
    risk.start();
    await risk.refreshWalletBalance();
    expect(risk.getSnapshot().walletBalanceSol).toBeCloseTo(1, 9);
    expect(risk.canEnter().ok).toBe(true);
    risk.stop();
  });

  it('dry-run still enforces the other breakers while the wallet floor stays quiet', () => {
    const h = harness({ mode: 'dry-run', risk: { dailyLossLimitSol: 1 } }, 0n);
    expect(h.risk.canEnter().ok).toBe(true); // floor quiet despite 0 real balance
    h.closed(-1.2);
    expect(h.risk.canEnter()).toMatchObject({ ok: false, reason: 'DAILY_LOSS' });
  });

  describe('NEGATIVE_EDGE (P4.3 edge monitor)', () => {
    const quiet = { consecutiveLossHalt: 1000, dailyLossLimitSol: 100, dailyLossLimitWalletPct: 100, emergencyExitCount24h: 1000 };

    it('is off by default: a losing streak never trips it', () => {
      const h = harness({ risk: quiet });
      for (let i = 0; i < 60; i++) h.closed(-0.05);
      expect(h.breakers).not.toContainEqual({ type: 'NEGATIVE_EDGE', tripped: true });
      expect(h.risk.getSnapshot().edge).toBeNull();
    });

    it('waits for minTrades, then pauses entries when the CI upper bound < 0', () => {
      const h = harness({ risk: { ...quiet, edgeMonitor: { enabled: true, minTrades: 10, window: 20, iterations: 500 } } });
      for (let i = 0; i < 9; i++) h.closed(i % 3 === 0 ? 0.01 : -0.05);
      expect(h.risk.canEnter().ok).toBe(true);
      h.closed(-0.05);
      expect(h.risk.canEnter()).toMatchObject({ ok: false, reason: 'NEGATIVE_EDGE' });
      expect(h.breakers).toContainEqual({ type: 'NEGATIVE_EDGE', tripped: true });
      const edge = h.risk.getSnapshot().edge!;
      expect(edge.n).toBe(10);
      expect(edge.ci!.hi).toBeLessThan(0);
    });

    it('does not trip while the CI still straddles zero', () => {
      const h = harness({ risk: { ...quiet, edgeMonitor: { enabled: true, minTrades: 10, iterations: 500 } } });
      for (let i = 0; i < 40; i++) h.closed(i % 2 === 0 ? 0.06 : -0.05);
      expect(h.risk.canEnter().ok).toBe(true);
      expect(h.risk.getSnapshot().edge!.negative).toBe(false);
    });

    it('rehydrates the window from the DB, restarting at an operator reset', () => {
      const repos = new Repositories(openDb({ path: ':memory:', memory: true }));
      const t = Date.UTC(2026, 6, 8, 12, 0, 0);
      for (let i = 0; i < 12; i++) {
        repos.upsertPosition({ mint: `a${i}`, state: 'CLOSED', sizeSol: 0.25, pnlSol: -0.05, closedAt: t - 3_600_000 + i });
      }
      const config = ConfigSchema.parse({ mode: 'paper', risk: { ...quiet, edgeMonitor: { enabled: true, minTrades: 10, iterations: 500 } } });
      const tripped = new RiskManager({ config, bus: new TypedBus(), repos, now: () => t });
      tripped.start();
      expect(tripped.canEnter()).toMatchObject({ ok: false, reason: 'NEGATIVE_EDGE' });
      tripped.stop();

      repos.recordRiskDayReset('test', t - 60_000);
      const cleared = new RiskManager({ config, bus: new TypedBus(), repos, now: () => t });
      cleared.start();
      expect(cleared.canEnter().ok).toBe(true);
      expect(cleared.getSnapshot().edge!.n).toBe(0);
      cleared.stop();
    });
  });
});
