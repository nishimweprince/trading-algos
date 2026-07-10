import { describe, it, expect } from 'vitest';
import { computePrice, PricePoller } from '../src/positions/pricing.ts';
import { evaluateExit, type ExitState } from '../src/exits/engine.ts';
import { PaperPosition } from '../src/positions/position.ts';
import { ConfigSchema } from '../src/config/schema.ts';

const CFG = ConfigSchema.parse({}).exits;

describe('computePrice', () => {
  it('is SOL per whole token', () => {
    // 1e9 tokens (decimals 6 -> 1e15 base units), 100 SOL reserve
    const price = computePrice(10n ** 15n, 100n * 10n ** 9n, 6);
    expect(price).toBeCloseTo(100 / 1e9, 15);
  });
  it('is zero for an empty base reserve', () => {
    expect(computePrice(0n, 100n, 6)).toBe(0);
  });
});

function tokenAccountBase64(amount: bigint): string {
  const buf = Buffer.alloc(72);
  buf.writeBigUInt64LE(amount, 64);
  return buf.toString('base64');
}

describe('PricePoller.readOnce', () => {
  it('reads both vaults once and computes a fresh pool price', async () => {
    const queried: string[][] = [];
    const rpc = {
      getMultipleAccountsBase64: async (addrs: string[]) => {
        queried.push(addrs);
        return [
          { data: tokenAccountBase64(10n ** 15n), owner: 'o', lamports: 0 },
          { data: tokenAccountBase64(200n * 10n ** 9n), owner: 'o', lamports: 0 },
        ];
      },
    };
    const poller = new PricePoller(rpc as never, 1000);

    const read = await poller.readOnce({ baseVault: 'base', quoteVault: 'quote', baseDecimals: 6 });

    expect(queried).toEqual([['base', 'quote']]);
    expect(read?.price).toBeCloseTo(2e-7, 15);
    expect(read?.baseReserve).toBe(10n ** 15n);
    expect(read?.quoteReserveLamports).toBe(200n * 10n ** 9n);
  });
});

function state(over: Partial<ExitState>): ExitState {
  return {
    entryPrice: 1,
    openedAtMs: 0,
    highWaterPrice: 1,
    tp0Done: false,
    tp1Done: false,
    trailingArmed: false,
    highVolatility: false,
    stopPrice: 0.8,
    ...over,
  };
}

describe('evaluateExit', () => {
  it('fires TP1 for a partial sell at +50%', () => {
    const d = evaluateExit(state({}), 1.5, 0, CFG);
    expect(d?.trigger).toBe('TAKE_PROFIT_1');
    expect(d?.sellFraction).toBe(CFG.tp1SellFraction);
  });
  it('fires TP2 for the remainder at +100% once TP1 done', () => {
    const d = evaluateExit(state({ tp1Done: true, stopPrice: 1.2 }), 2.0, 0, CFG);
    expect(d?.trigger).toBe('TAKE_PROFIT_2');
    expect(d?.sellFraction).toBe(1);
  });
  it('fires STOP_LOSS at the stop price', () => {
    expect(evaluateExit(state({}), 0.79, 0, CFG)?.trigger).toBe('STOP_LOSS');
  });
  it('fires TRAILING_STOP below the high-water gap', () => {
    const d = evaluateExit(state({ trailingArmed: true, highWaterPrice: 1.3 }), 1.3 * 0.84, 0, CFG);
    expect(d?.trigger).toBe('TRAILING_STOP');
  });
  it('high-volatility uses the tighter trail (10%) where the normal 15% would hold', () => {
    // A 12% pullback from the high: inside the 15% gap (no exit) but past the
    // 10% high-vol gap — so the momentum-driven highVolatility flag must fire it.
    const price = 1.3 * 0.88;
    expect(evaluateExit(state({ trailingArmed: true, highWaterPrice: 1.3 }), price, 0, CFG)).toBeNull();
    const hv = evaluateExit(
      state({ trailingArmed: true, highWaterPrice: 1.3, highVolatility: true }),
      price,
      0,
      CFG,
    );
    expect(hv?.trigger).toBe('TRAILING_STOP');
  });
  it('fires TIME_STOP after the window with no TP1', () => {
    const d = evaluateExit(state({}), 1.0, CFG.timeStopMinutes * 60_000, CFG);
    expect(d?.trigger).toBe('TIME_STOP');
  });
  it('does nothing mid-range', () => {
    expect(evaluateExit(state({}), 1.1, 0, CFG)).toBeNull();
  });

  it('does not fire TP0 when disabled (default)', () => {
    expect(evaluateExit(state({}), 1.3, 0, CFG)).toBeNull();
  });

  it('fires TP0 partial at +30% when enabled, then still TP1 at +50%', () => {
    const cfg = { ...CFG, tp0Enabled: true };
    const d0 = evaluateExit(state({}), 1.3, 0, cfg);
    expect(d0?.trigger).toBe('TAKE_PROFIT_0');
    expect(d0?.sellFraction).toBeCloseTo(cfg.tp0SellFraction, 6);
    // After TP0 is booked, TP1 still fires at +50%.
    const d1 = evaluateExit(state({ tp0Done: true }), 1.5, 0, cfg);
    expect(d1?.trigger).toBe('TAKE_PROFIT_1');
    // TP0 does not re-fire once booked.
    expect(evaluateExit(state({ tp0Done: true }), 1.3, 0, cfg)).toBeNull();
  });
});

describe('PaperPosition lifecycle', () => {
  const mk = () => new PaperPosition({ mint: 'M', sizeSol: 1, entryPrice: 1, openedAtMs: 0, highVolatility: false, cfg: CFG });

  it('TP1 then TP2 realizes staged PnL and closes', () => {
    const p = mk();
    const f1 = p.onPrice(1.5, 1000);
    expect(f1[0]?.trigger).toBe('TAKE_PROFIT_1');
    expect(p.remainingFraction).toBeCloseTo(0.25, 6);
    expect(p.state).toBe('OPEN');

    const f2 = p.onPrice(2.0, 2000);
    expect(f2[0]?.trigger).toBe('TAKE_PROFIT_2');
    expect(p.state).toBe('CLOSED');
    // 0.75 @ +50% + 0.25 @ +100% = 0.375 + 0.25
    expect(p.realizedPnlSol).toBeCloseTo(0.625, 6);
  });

  it('hard stop closes at a loss', () => {
    const p = mk();
    const f = p.onPrice(0.8, 500);
    expect(f[0]?.trigger).toBe('STOP_LOSS');
    expect(p.state).toBe('CLOSED');
    expect(p.realizedPnlSol).toBeCloseTo(-0.2, 6);
  });

  it('after TP1 the remainder stop moves up to +20%', () => {
    const p = mk();
    p.onPrice(1.5, 1000); // TP1, stop -> 1.2
    const f = p.onPrice(1.2, 2000); // hits the raised stop
    expect(f[0]?.trigger).toBe('STOP_LOSS');
    expect(p.state).toBe('CLOSED');
    // remainder 0.25 exits at +20%: 0.375 + 0.25*0.2 = 0.425
    expect(p.realizedPnlSol).toBeCloseTo(0.425, 6);
  });

  it('trailing stop exits after arming', () => {
    const p = mk();
    expect(p.onPrice(1.3, 1000)).toHaveLength(0); // arms trailing at +30%, no exit
    const f = p.onPrice(1.3 * 0.84, 2000);
    expect(f[0]?.trigger).toBe('TRAILING_STOP');
    expect(p.state).toBe('CLOSED');
  });

  it('time stop closes a stagnant position', () => {
    const p = mk();
    const f = p.onPrice(1.0, CFG.timeStopMinutes * 60_000 + 1);
    expect(f[0]?.trigger).toBe('TIME_STOP');
    expect(p.state).toBe('CLOSED');
  });

  it('tighter new trail (arm20/gap12) captures more of a sub-TP1 peak than old (arm25/gap15)', () => {
    // A token that peaks at +30% then rolls over — the shape of the live trade
    // that netted only +7% under the old 15% gap.
    // CFG carries the researched schema defaults (arm25/gap15); config.yaml now
    // overrides them to arm20/gap12 in production. Compare the two explicitly.
    const newCfg = { ...CFG, trailingArmPct: 20, trailingGapPct: 12 };
    const newP = new PaperPosition({ mint: 'M', sizeSol: 1, entryPrice: 1, openedAtMs: 0, highVolatility: false, cfg: newCfg });
    const oldP = new PaperPosition({ mint: 'M', sizeSol: 1, entryPrice: 1, openedAtMs: 0, highVolatility: false, cfg: CFG });
    newP.onPrice(1.3, 1000);
    oldP.onPrice(1.3, 1000);
    // At 1.14 the new 12% gap (threshold 1.144) exits; the old 15% gap (1.105) still holds.
    const fNew = newP.onPrice(1.14, 2000);
    const fOld = oldP.onPrice(1.14, 2000);
    expect(fNew[0]?.trigger).toBe('TRAILING_STOP');
    expect(newP.state).toBe('CLOSED');
    expect(fOld).toHaveLength(0); // old config gives back more before exiting
    expect(newP.realizedPnlSol).toBeCloseTo(0.14, 6);
  });

  it('TP0 (opt-in) banks a partial at +30% and raises the stop on the remainder', () => {
    const cfg = { ...CFG, tp0Enabled: true };
    const p = new PaperPosition({ mint: 'M', sizeSol: 1, entryPrice: 1, openedAtMs: 0, highVolatility: false, cfg });
    const f0 = p.onPrice(1.3, 1000);
    expect(f0[0]?.trigger).toBe('TAKE_PROFIT_0');
    expect(p.remainingFraction).toBeCloseTo(1 - cfg.tp0SellFraction, 6);
    expect(p.state).toBe('OPEN');
    // Stop moved up to +10%; a drop to entry no longer round-trips to a loss.
    const f1 = p.onPrice(1.1, 2000);
    expect(f1[0]?.trigger).toBe('STOP_LOSS');
    expect(p.state).toBe('CLOSED');
    // 0.33 @ +30% + 0.67 @ +10% = 0.099 + 0.067
    expect(p.realizedPnlSol).toBeCloseTo(0.33 * 0.3 + 0.67 * 0.1, 6);
  });
});
