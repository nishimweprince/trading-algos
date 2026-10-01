import { describe, it, expect } from 'vitest';
import { openDb } from '../src/persistence/db.ts';
import { Repositories } from '../src/persistence/repositories.ts';
import { ShadowTracker } from '../src/guardrails/shadow.ts';
import { Simulator, type SimulatorCfg } from '../src/positions/simulator.ts';
import type { RpcClient } from '../src/core/rpc.ts';
import type { PoolRef } from '../src/positions/pricing.ts';
import { ConfigSchema } from '../src/config/schema.ts';

const stubRpc = { getMultipleAccountsBase64: async () => [] } as unknown as RpcClient;
const poolRef = (mint: string): PoolRef => ({ mint, baseVault: `${mint}-base`, quoteVault: `${mint}-quote`, baseDecimals: 6 });
const CFG = ConfigSchema.parse({});

const SIM_CFG: SimulatorCfg = {
  enabled: true,
  seed: 7,
  minSamples: 1,
  entryConfirmMedianMs: 644,
  entryConfirmP90Ms: 1097,
  exitConfirmMedianMs: 1257,
  exitConfirmP90Ms: 3143,
  entryHaircutPct: { min: 0, mode: 0, max: 0 },
  baseEntryFailPct: 0,
};

function tracker(repos: Repositories, now: () => number, sizeSol: number, exits = CFG.exits) {
  const sim = new Simulator(SIM_CFG);
  sim.setSamples('entry_confirm', [2000]);
  sim.setSamples('exit_confirm', [0]);
  return new ShadowTracker(stubRpc, repos, {
    windowMs: 10_000,
    sizeSol,
    exits,
    fees: CFG.fees,
    simulator: sim,
    now,
  });
}

function outcome(db: ReturnType<typeof openDb>, mint: string) {
  return db.prepare(`SELECT * FROM shadow_outcomes WHERE mint = ?`).get(mint) as Record<string, number | string | null>;
}

describe('shadow honest fills (v3 amm)', () => {
  it('opens at the first tick at/after the sampled entry latency, not at baseline', () => {
    const db = openDb({ path: ':memory:', memory: true });
    const repos = new Repositories(db);
    let now = 0;
    const t = tracker(repos, () => now, 0.04);
    t.track({ mint: 'deferred', verdict: 'veto', primaryVetoCode: 'H7', baselinePrice: 1, poolRef: poolRef('deferred') });

    now = 1000;
    t.injectTick('deferred', 1.0, now);
    expect(t.size).toBe(1);

    now = 2000;
    t.injectTick('deferred', 1.0, now); // entry opens here
    now = 10_000;
    t.injectTick('deferred', 1.0, now); // window expiry -> TIME_STOP force-close
    expect(t.size).toBe(0);

    const row = outcome(db, 'deferred');
    expect(row.exit_reason).toBe('TIME_STOP');
    expect(row.hold_ms).toBe(8000); // opened at t=2000, not at baseline t=0
    expect(row.outcome_version).toBe('exit_fsm_v3_amm');
    t.stop();
    db.close();
  });

  it('records ENTRY_FAILED with zero PnL when the mid moves past slippage', () => {
    const db = openDb({ path: ':memory:', memory: true });
    const repos = new Repositories(db);
    let now = 0;
    const t = tracker(repos, () => now, 0.04);
    t.track({ mint: 'snipe', verdict: 'veto', primaryVetoCode: 'H7', baselinePrice: 1, poolRef: poolRef('snipe') });

    now = 2000;
    t.injectTick('snipe', 2.5, now); // +150% before entry can land (CCPob's H4 probe)
    expect(t.size).toBe(0);

    const row = outcome(db, 'snipe');
    expect(row.exit_reason).toBe('ENTRY_FAILED');
    expect(row.net_pnl_sol).toBe(0);
    expect(row.gross_pnl_sol).toBe(0);
    expect(row.peak_mfe_pct).toBeCloseTo(150, 9); // the coin's move, not our fill
    t.stop();
    db.close();
  });

  it('caps a tiny-pool moon below the pool SOL (CCPob artifact gone)', () => {
    const db = openDb({ path: ':memory:', memory: true });
    const repos = new Repositories(db);
    let now = 0;
    // No take-profits / trailing so the whole position exits in one force-close leg.
    const quiet = { ...CFG.exits, tp0Pct: 10_000, tp1Pct: 20_000, trailingArmPct: 10_000, timeStopMinutes: 60 };
    const t = tracker(repos, () => now, 0.04, quiet);
    t.track({
      mint: 'dust', verdict: 'veto', primaryVetoCode: 'H7', baselinePrice: 1,
      quoteReserveSol: 0.28, poolRef: poolRef('dust'),
    });

    now = 2000;
    t.injectTick('dust', 1.0, now, 0.28); // entry pays 1 + 0.04/0.28 impact
    now = 5000;
    t.injectTick('dust', 70.0, now, 0.28); // +6900% holographic pump
    now = 10_000;
    t.injectTick('dust', 70.0, now, 0.28); // window expiry -> force-close
    expect(t.size).toBe(0);

    const row = outcome(db, 'dust');
    expect(row.gross_pnl_sol as number).toBeLessThan(0.28);
    expect(row.net_pnl_sol as number).toBeLessThan(0.28);
    t.stop();
    db.close();
  });

  it('is almost unchanged on a 70 SOL pool (< 1 point)', () => {
    const run = (reserve: number | undefined) => {
      const db = openDb({ path: ':memory:', memory: true });
      const repos = new Repositories(db);
      let now = 0;
      const quiet = { ...CFG.exits, tp0Pct: 10_000, tp1Pct: 20_000, trailingArmPct: 10_000, timeStopMinutes: 60 };
      const t = tracker(repos, () => now, 0.04, quiet);
      t.track({
        mint: 'deep', verdict: 'veto', primaryVetoCode: 'H7', baselinePrice: 1,
        ...(reserve !== undefined ? { quoteReserveSol: reserve } : {}),
        poolRef: poolRef('deep'),
      });
      now = 2000;
      t.injectTick('deep', 1.0, now, reserve ?? 70);
      now = 5000;
      t.injectTick('deep', 2.0, now, reserve ?? 70);
      now = 10_000;
      t.injectTick('deep', 2.0, now, reserve ?? 70);
      const row = outcome(db, 'deep');
      t.stop();
      db.close();
      return row.pnl_pct as number;
    };
    // Impact on a 70 SOL pool is ~0.06% entry, ~0.1% exit — far under a point.
    expect(Math.abs(run(70) - run(undefined))).toBeLessThan(1);
  });
});
