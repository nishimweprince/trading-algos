import { describe, expect, it } from 'vitest';
import { ConfigSchema } from '../src/config/schema.ts';
import { evaluateExit, exitCfgFor, type ExitState } from '../src/exits/engine.ts';
import { PaperPosition } from '../src/positions/position.ts';

const base = (over: Partial<ExitState> = {}): ExitState => ({
  entryPrice: 1,
  openedAtMs: 0,
  highWaterPrice: 1,
  tp0Done: false,
  tp1Done: false,
  trailingArmed: false,
  highVolatility: false,
  stopPrice: 0.85,
  ...over,
});

describe('dead-money exit', () => {
  const cfg = ConfigSchema.parse({ exits: { deadMoneyEnabled: true, deadMoneyMinutes: 3, deadMoneyMaxMfePct: 5 } }).exits;

  it('does nothing before the deadline', () => {
    expect(evaluateExit(base({ highWaterPrice: 1.02 }), 1.01, 2 * 60_000, cfg)).toBeNull();
  });
  it('closes a flat position at the deadline as TIME_STOP with a dead-money reason', () => {
    const d = evaluateExit(base({ highWaterPrice: 1.03 }), 1.01, 3 * 60_000, cfg);
    expect(d?.trigger).toBe('TIME_STOP');
    expect(d?.sellFraction).toBe(1);
    expect(d?.reason).toMatch(/dead money/);
  });
  it('spares a position whose peak cleared the bar, even if it has since faded', () => {
    expect(evaluateExit(base({ highWaterPrice: 1.08 }), 1.01, 3 * 60_000, cfg)).toBeNull();
  });
  it('never fires once a partial has banked', () => {
    expect(evaluateExit(base({ tp0Done: true, highWaterPrice: 1.03 }), 1.01, 3 * 60_000, cfg)).toBeNull();
  });
  it('is off by default', () => {
    const off = ConfigSchema.parse({}).exits;
    expect(off.deadMoneyEnabled).toBe(false);
    expect(evaluateExit(base({ highWaterPrice: 1.01 }), 1.0, 5 * 60_000, off)).toBeNull();
  });
  it('drives a PaperPosition to CLOSED through the ordinary FSM', () => {
    const pos = new PaperPosition({ mint: 'M', sizeSol: 0.1, entryPrice: 1, openedAtMs: 0, highVolatility: false, cfg });
    expect(pos.onPrice(1.02, 60_000)).toHaveLength(0);
    const fills = pos.onPrice(1.01, 3 * 60_000 + 1);
    expect(fills).toHaveLength(1);
    expect(fills[0]!.trigger).toBe('TIME_STOP');
    expect(pos.state).toBe('CLOSED');
  });
});

describe('exitCfgFor with twin overrides', () => {
  const config = ConfigSchema.parse({
    exits: { tp1Pct: 40, timeStopMinutes: 10, trailingGapPct: 8 },
    guardrails: { relaxedRiskTimeStopMinutes: 6, relaxedRiskTrailingGapPct: 10 },
  });

  it('returns the live rules untouched without overrides', () => {
    expect(exitCfgFor(config, false)).toBe(config.exits);
  });
  it('applies only the keys that are set', () => {
    const cfg = exitCfgFor(config, false, { tp1Pct: 30, deadMoneyEnabled: true });
    expect(cfg.tp1Pct).toBe(30);
    expect(cfg.deadMoneyEnabled).toBe(true);
    expect(cfg.timeStopMinutes).toBe(10);
    expect(cfg.trailingGapPct).toBe(8);
  });
  it('still tightens relaxed-risk accepts relative to the variant', () => {
    const cfg = exitCfgFor(config, true, { timeStopMinutes: 4, trailingGapPct: 12 });
    expect(cfg.timeStopMinutes).toBe(4); // min(variant 4, relaxed 6)
    expect(cfg.trailingGapPct).toBe(10); // min(variant 12, relaxed 10)
  });
  it('is validated by the schema: unknown keys are rejected', () => {
    expect(() => ConfigSchema.parse({ dryRunTwin: { exitOverrides: { nope: 1 } } })).toThrow();
    const ok = ConfigSchema.parse({ dryRunTwin: { exitOverrides: { deadMoneyEnabled: true } } });
    expect(ok.dryRunTwin.exitOverrides).toEqual({ deadMoneyEnabled: true });
  });
});
