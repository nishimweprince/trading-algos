import { describe, expect, it, vi } from 'vitest';
import {
  buySlippageAttempts,
  isExceededSlippage,
  PUMP_AMM_EXCEEDED_SLIPPAGE,
  withSlippageRetry,
} from '../src/executor/slippage.ts';

describe('isExceededSlippage', () => {
  it('matches Pump AMM Custom 6004', () => {
    expect(isExceededSlippage({ InstructionError: [6, { Custom: PUMP_AMM_EXCEEDED_SLIPPAGE }] })).toBe(true);
  });

  it('matches the named ExceededSlippage error', () => {
    expect(isExceededSlippage({ InstructionError: [6, 'ExceededSlippage'] })).toBe(true);
    expect(isExceededSlippage(new Error('ExceededSlippage'))).toBe(true);
  });

  it('does not match other program errors', () => {
    expect(isExceededSlippage({ InstructionError: [6, { Custom: 6003 }] })).toBe(false);
    expect(isExceededSlippage({ InstructionError: [0, 'Custom'] })).toBe(false);
    expect(isExceededSlippage(null)).toBe(false);
    expect(isExceededSlippage(undefined)).toBe(false);
  });
});

describe('buySlippageAttempts', () => {
  it('starts at maxSlippagePct then walks looser exit-ladder rungs', () => {
    expect(buySlippageAttempts(5, [2, 5, 10, 25])).toEqual([5, 10, 25]);
  });

  it('does not add tighter or duplicate rungs', () => {
    expect(buySlippageAttempts(25, [2, 5, 10, 25])).toEqual([25]);
    expect(buySlippageAttempts(10, [10, 10, 25])).toEqual([10, 25]);
  });
});

describe('withSlippageRetry', () => {
  it('rebuilds at the next rung when simulation hits ExceededSlippage and nothing was sent', async () => {
    const run = vi.fn()
      .mockResolvedValueOnce({ sent: false, simErr: { InstructionError: [6, { Custom: 6004 }] } })
      .mockResolvedValueOnce({ sent: true, confirmed: true, simErr: undefined });

    const retries: number[] = [];
    const result = await withSlippageRetry([5, 10, 25], run, {
      onRetry: (next) => retries.push(next),
    });

    expect(run).toHaveBeenCalledTimes(2);
    expect(run.mock.calls.map((c) => c[0])).toEqual([5, 10]);
    expect(retries).toEqual([10]);
    expect(result).toMatchObject({ sent: true, confirmed: true });
  });

  it('does not retry a non-slippage simulation failure', async () => {
    const fail = { sent: false, simErr: { InstructionError: [3, { Custom: 6003 }] } };
    const run = vi.fn().mockResolvedValue(fail);
    const result = await withSlippageRetry([5, 10, 25], run);
    expect(run).toHaveBeenCalledOnce();
    expect(result).toBe(fail);
  });

  it('does not retry after a send even if simErr is set', async () => {
    const sent = { sent: true, simErr: { InstructionError: [6, { Custom: 6004 }] } };
    const run = vi.fn().mockResolvedValue(sent);
    await withSlippageRetry([5, 10], run);
    expect(run).toHaveBeenCalledOnce();
  });
});
