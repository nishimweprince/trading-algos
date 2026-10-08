import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';
import { openDb } from '../src/persistence/db.ts';
import { Repositories } from '../src/persistence/repositories.ts';
import { getRacerSummary } from '../src/dashboard/queries.ts';
import { PaperTracker, type PaperTrack } from '../src/racer/paper.ts';
import { paperSizeFromBalance } from '../src/racer/walletSize.ts';
import { Wallet } from '../src/executor/wallet.ts';

const OPTS = { floorSol: 0.033, headroomSol: 0.003, minSol: 0.01, maxSol: 0.1, walletConfigured: true };

describe('paperSizeFromBalance', () => {
  it('sizes the max when funded', () => {
    expect(paperSizeFromBalance(10, OPTS)).toEqual({ sizeSol: 0.1, walletBalanceSol: 10, reason: 'ok' });
    expect(paperSizeFromBalance(0.136, OPTS)).toEqual({ sizeSol: 0.1, walletBalanceSol: 0.136, reason: 'ok' });
  });
  it('sizes from the balance when the wallet is small but above the floor', () => {
    expect(paperSizeFromBalance(0.1, OPTS)).toEqual({
      sizeSol: 0.1 - 0.033 - 0.003,
      walletBalanceSol: 0.1,
      reason: 'sized-by-balance',
    });
  });
  it('tracks at the min, flagged, below the floor or under min available', () => {
    expect(paperSizeFromBalance(0.02, OPTS).reason).toBe('below-floor');
    expect(paperSizeFromBalance(0.02, OPTS).sizeSol).toBe(0.01);
    expect(paperSizeFromBalance(0.04, OPTS)).toEqual({ sizeSol: 0.01, walletBalanceSol: 0.04, reason: 'below-floor' });
  });
  it('falls back to the max, flagged, with no wallet or an unreadable balance', () => {
    expect(paperSizeFromBalance(1, { ...OPTS, walletConfigured: false })).toEqual({
      sizeSol: 0.1,
      walletBalanceSol: null,
      reason: 'no-wallet',
    });
    for (const bad of [null, Number.NaN, -1]) {
      expect(paperSizeFromBalance(bad, OPTS)).toEqual({ sizeSol: 0.1, walletBalanceSol: null, reason: 'balance-unreadable' });
    }
  });
});

describe('PaperTracker wallet-sized tracks', () => {
  it('carries a per-track size, balance and reason through to the sink', () => {
    const sunk: PaperTrack[] = [];
    const p = new PaperTracker(
      { readMark: async () => null, sink: (t) => void sunk.push(t) },
      { horizonsSec: [], sizeSol: 0.1 },
    );
    expect(
      p.track('M', {
        poolAddress: 'P',
        slot: 1,
        sizeSol: 0.05,
        walletBalanceSol: 0.09,
        sizingReason: 'sized-by-balance',
        open: null,
        reach: { price: 1, quoteSol: 80 },
      }),
    ).toBe(true);
    expect(sunk).toHaveLength(1);
    expect(sunk[0]).toMatchObject({ sizeSol: 0.05, walletBalanceSol: 0.09, sizingReason: 'sized-by-balance' });
  });
  it('defaults to the fixed size with no wallet fields', () => {
    const sunk: PaperTrack[] = [];
    const p = new PaperTracker(
      { readMark: async () => null, sink: (t) => void sunk.push(t) },
      { horizonsSec: [], sizeSol: 0.1 },
    );
    p.track('M', { poolAddress: 'P', slot: 1, open: null, reach: { price: 1, quoteSol: 80 } });
    expect(sunk[0]).toMatchObject({ sizeSol: 0.1, walletBalanceSol: null, sizingReason: null });
  });
});

describe('racer wallet-sized summary', () => {
  it('reports sizing cohorts and a wallet-sized curve at actual sizes', () => {
    const db = openDb({ path: ':memory:', memory: true });
    const repos = new Repositories(db);
    for (const [mint, sizeSol, balance, reason, pct] of [
      ['A', 0.1, 10, 'ok', 20],
      ['B', 0.05, 0.09, 'sized-by-balance', -10],
    ] as const) {
      repos.recordRacerPaperTrack({
        mint,
        poolAddress: `pool-${mint}`,
        entrySlot: 1,
        sizeSol,
        walletBalanceSol: balance,
        sizingReason: reason,
        openPrice: 1,
        openQuoteSol: 80,
        reachPrice: 1,
        reachQuoteSol: 80,
        horizonsJson: JSON.stringify([{ sec: 30, price: 1, quoteSol: 80, pnlOpenPct: pct, pnlReachPct: pct, pnl: { slot1mid: pct } }]),
        armed: true,
        entryModelVersion: 2,
        entryQuality: 'verified',
      });
    }
    const s = getRacerSummary(db);
    expect(s.paper.sizing.reasons).toEqual({ ok: 1, 'sized-by-balance': 1 });
    expect(s.paper.sizing.medianWalletBalanceSol).toBeCloseTo(5.045, 6);
    expect(s.paper.sizing.medianSizeSol).toBeCloseTo(0.075, 6);
    const h = s.paper.horizons[0]!;
    // No repricing: A scored +20 at 0.1, B −10 at 0.05.
    expect(h.headline.walletSizePnlCurve.map((p) => p.mint)).toEqual(['A', 'B']);
    expect(h.headline.cumulativeWalletSizePnlSol).toBeCloseTo(0.02 - 0.005, 6);
  });
});

describe('racer no-send guard', () => {
  it('signs the send-ready probe with an ephemeral key, never the wallet', () => {
    expect(Wallet.load('RACER_WALLET_SIZE_TEST_UNSET_VAR', 'dry-run').ephemeral).toBe(true);
    const worker = readFileSync(new URL('../src/racer-worker.ts', import.meta.url), 'utf8');
    expect(worker).toContain(`Wallet.load('RACER_PROBE_NO_KEY', 'dry-run')`);
  });
  it('has no broadcast/send path in the worker or the paper modules', () => {
    const worker = readFileSync(new URL('../src/racer-worker.ts', import.meta.url), 'utf8');
    expect(worker).toMatch(/import \{ randomSenderTipAccount \} from '\.\/executor\/heliusSender\.ts';/);
    for (const token of ['Broadcaster', 'sendTransaction', 'signAndSend', '.send(']) {
      expect(worker).not.toContain(token);
    }
    for (const file of ['../src/racer/paper.ts', '../src/racer/walletSize.ts']) {
      const src = readFileSync(new URL(file, import.meta.url), 'utf8');
      for (const token of ['Broadcaster', 'sendTransaction', 'signAndSend', 'Wallet.load', 'getBalance', '.send(']) {
        expect(src).not.toContain(token);
      }
    }
  });
});
