import { describe, it, expect } from 'vitest';
import { EmergencyMonitor } from '../src/positions/monitors.ts';

const CFG = { lpDropPct: 15, windowTicks: 5, creatorDumpEnabled: true, creatorDumpPct: 50 };
const sol = (n: number) => BigInt(Math.floor(n * 1e9));

describe('EmergencyMonitor — LP pull', () => {
  it('does not fire on a small / gradual drop', () => {
    const m = new EmergencyMonitor(CFG);
    expect(m.onTick({ quoteReserveLamports: sol(100) })).toBeNull();
    expect(m.onTick({ quoteReserveLamports: sol(96) })).toBeNull(); // -4%
    expect(m.onTick({ quoteReserveLamports: sol(90) })).toBeNull(); // -10% from high
  });

  it('fires LP_PULL on a sharp drop from the window high', () => {
    const m = new EmergencyMonitor(CFG);
    m.onTick({ quoteReserveLamports: sol(100) });
    m.onTick({ quoteReserveLamports: sol(100) });
    const sig = m.onTick({ quoteReserveLamports: sol(83) }); // -17%
    expect(sig?.kind).toBe('LP_PULL');
  });
});

describe('EmergencyMonitor — creator dump', () => {
  it('fires CREATOR_DUMP when the dev sells past the threshold', () => {
    const m = new EmergencyMonitor(CFG);
    expect(m.onTick({ quoteReserveLamports: sol(100), creatorBaseBalance: 1000n })).toBeNull(); // baseline
    expect(m.onTick({ quoteReserveLamports: sol(100), creatorBaseBalance: 700n })).toBeNull(); // -30%
    const sig = m.onTick({ quoteReserveLamports: sol(100), creatorBaseBalance: 400n }); // -60%
    expect(sig?.kind).toBe('CREATOR_DUMP');
  });

  it('never fires without a creator balance', () => {
    const m = new EmergencyMonitor(CFG);
    for (let i = 0; i < 5; i++) expect(m.onTick({ quoteReserveLamports: sol(100) })).toBeNull();
  });

  it('respects the disabled flag', () => {
    const m = new EmergencyMonitor({ ...CFG, creatorDumpEnabled: false });
    m.onTick({ quoteReserveLamports: sol(100), creatorBaseBalance: 1000n });
    expect(m.onTick({ quoteReserveLamports: sol(100), creatorBaseBalance: 10n })).toBeNull();
  });
});

/**
 * Regression guard for the 2026-09-09 emergency-exit deadlock.
 *
 * At lpDropPct 15 / windowTicks 5 the monitor fired on 10 of 24 paper positions
 * whose MAE was exactly 0.00% — the price never ticked below entry on any of
 * them. Each fire blacklisted the creator and counted toward
 * risk.emergencyExitCount24h, which tripped and vetoed 574/655 graduations on
 * H10 for the rest of the day.
 *
 * Two causes, both covered here:
 *  1. 15% of a 25-SOL floor pool is 3.75 SOL — two or three ordinary exits.
 *  2. PricePoller.poll() silently skips a tick while the previous request is in
 *     flight, so a 5-tick window stretched across minutes of wall time and any
 *     normal drift filled it.
 */
describe('EmergencyMonitor — shipped LP thresholds (see config.yaml exits.*)', () => {
  const SHIPPED = { lpDropPct: 35, windowTicks: 20, creatorDumpEnabled: true, creatorDumpPct: 50 };

  it('tolerates ordinary post-graduation sell flow on a floor-size pool', () => {
    const m = new EmergencyMonitor(SHIPPED);
    // 25 SOL pool (guardrails.minPoolSol) draining ~30% through normal exits.
    for (const reserve of [25, 24.1, 23.4, 22.0, 21.2, 20.3, 19.4, 18.6, 17.8, 17.6]) {
      expect(m.onTick({ quoteReserveLamports: sol(reserve) })).toBeNull();
    }
  });

  it('still fires on a genuine liquidity pull', () => {
    const m = new EmergencyMonitor(SHIPPED);
    m.onTick({ quoteReserveLamports: sol(25) });
    m.onTick({ quoteReserveLamports: sol(24) });
    expect(m.onTick({ quoteReserveLamports: sol(16) })?.kind).toBe('LP_PULL'); // -36% from high
  });

  it('holds 20 ticks of history, so dropped polls cannot shrink the window', () => {
    const m = new EmergencyMonitor(SHIPPED);
    m.onTick({ quoteReserveLamports: sol(100) }); // window high
    // 19 more ticks drifting gently down — the high must still be in the window.
    for (let i = 1; i < 20; i++) {
      expect(m.onTick({ quoteReserveLamports: sol(100 - i * 0.5) })).toBeNull();
    }
    // Tick 21 evicts the 100-SOL high; the window max is now ~90.5, so a drop to
    // 70 is -22.6% against it and must NOT fire, while the old 5-tick window
    // would have compared against a far more recent high.
    expect(m.onTick({ quoteReserveLamports: sol(70) })).toBeNull();
  });
});
