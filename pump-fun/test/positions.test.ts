import { describe, it, expect } from 'vitest';
import { computePrice } from '../src/positions/pricing.ts';
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

function state(over: Partial<ExitState>): ExitState {
  return {
    entryPrice: 1,
    openedAtMs: 0,
    highWaterPrice: 1,
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
  it('fires TIME_STOP after the window with no TP1', () => {
    const d = evaluateExit(state({}), 1.0, CFG.timeStopMinutes * 60_000, CFG);
    expect(d?.trigger).toBe('TIME_STOP');
  });
  it('does nothing mid-range', () => {
    expect(evaluateExit(state({}), 1.1, 0, CFG)).toBeNull();
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
});
