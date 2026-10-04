import { describe, expect, it } from 'vitest';
import { simulateTrade, buildPricePath, type PricePoint } from '../src/research/poolReplay.ts';
import { renderReplayMarkdown } from '../src/research/pool-replay-cli.ts';
import { ConfigSchema } from '../src/config/schema.ts';
import { exitCfgFor } from '../src/exits/engine.ts';
import { WSOL_MINT } from '../src/core/constants.ts';
import type { ParsedTx } from '../src/core/rpc.ts';

const VIRTUAL = 17_584_505_324n;
const BASE = 206_900_000_000_000n;
const config = ConfigSchema.parse({ mode: 'paper', exits: { hardStopPct: 15, tp1Pct: 15, tp1SellFraction: 1, timeStopMinutes: 10 } });
const cfg = { exits: exitCfgFor(config, false), feeBps: 125, txCostSol: 0.0000725, horizonMs: 600_000 };

/** A path whose effective price moves by `moves` (% from open), one point per slot. */
function path(moves: number[]): PricePoint[] {
  const open = Number(67_405_855_261n + VIRTUAL);
  return moves.map((m, i) => {
    const eff = open * (1 + m / 100);
    const quoteVault = BigInt(Math.round(eff)) - VIRTUAL;
    return { signature: `s${i}`, slot: 100 + i, index: i, signer: null, quoteVault, baseVault: BASE, price: eff / 1e9 / (Number(BASE) / 1e6), err: false };
  });
}

describe('pool replay', () => {
  it('takes profit when the effective price runs past TP1 net of fees', () => {
    const o = simulateTrade(path([0, 5, 12, 25, 30]), 0, 0.02, cfg, VIRTUAL);
    expect(o.exitReason).toBe('TAKE_PROFIT_1');
    expect(o.netPct).toBeGreaterThan(5);
  });

  it('stops out on a real 20 % drop, and only then', () => {
    expect(simulateTrade(path([0, -3, -5, -2, 1]), 0, 0.02, cfg, VIRTUAL).exitReason).toBe('HORIZON');
    expect(simulateTrade(path([0, -10, -20]), 0, 0.02, cfg, VIRTUAL).exitReason).toBe('STOP_LOSS');
  });

  it('a flat pool costs the round trip (2 x fee + tx costs)', () => {
    const o = simulateTrade(path([0, 0, 0]), 0, 0.02, cfg, VIRTUAL);
    expect(o.netPct).toBeLessThan(-2.5);
    expect(o.netPct).toBeGreaterThan(-4);
  });

  it('builds the path on vault + virtual from the pool vault post balances', () => {
    const tx = (sig: string, slot: number, quote: string): ParsedTx => ({
      slot,
      blockTime: null,
      meta: { err: null, preBalances: [], postBalances: [], postTokenBalances: [
        { accountIndex: 1, mint: WSOL_MINT, owner: 'POOL', uiTokenAmount: { amount: quote, decimals: 9 } },
        { accountIndex: 2, mint: 'MINT', owner: 'POOL', uiTokenAmount: { amount: BASE.toString(), decimals: 6 } },
      ] },
      transaction: { signatures: [sig], message: { accountKeys: [{ pubkey: 'S', signer: true }] } },
    });
    const p = buildPricePath([tx('a', 1, '67405855261')], 'POOL', 'MINT', VIRTUAL, 6);
    expect(p[0]!.price).toBeCloseTo(4.108e-7, 9);
  });

  it('renders a summary table', () => {
    const o = simulateTrade(path([0, 20]), 0, 0.02, cfg, VIRTUAL);
    const md = renderReplayMarkdown([{ 'ours_0.02': o, 'first_0.02': o, actualNetSol: -0.001, ourSlotOffset: 1 }], [0.02], cfg);
    expect(md).toContain('| our landed position | 0.02 | 1 |');
    expect(md).toContain('+1: 1');
  });
});
