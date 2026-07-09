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
