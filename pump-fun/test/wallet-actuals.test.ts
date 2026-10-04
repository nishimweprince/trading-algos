import { describe, expect, it, vi } from 'vitest';
import { combineTradeActuals, parseFillActuals, type FillActuals } from '../src/executor/fillActuals.ts';
import type { ParsedTx } from '../src/core/rpc.ts';
import { ActualsRecorder } from '../src/positions/actuals.ts';
import { Repositories } from '../src/persistence/repositories.ts';
import { openDb } from '../src/persistence/db.ts';
import { computeWalletReconciliation } from '../src/persistence/walletReconciliation.ts';
import { loadClosedPnls } from '../src/dashboard/analytics.ts';
import { getDashboardSummary } from '../src/dashboard/queries.ts';
import { ConfigSchema } from '../src/config/schema.ts';

const WALLET = 'Wallet111';
const ATA = 'Ata111';
const MINT = 'Mint111';

function tx(opts: { pre: number[]; post: number[]; keys: string[]; fee?: number; preTok?: string; postTok?: string; err?: unknown }): ParsedTx {
  const tok = (amount: string | undefined) =>
    amount === undefined ? [] : [{ accountIndex: opts.keys.indexOf(ATA), mint: MINT, owner: WALLET, uiTokenAmount: { amount, decimals: 6 } }];
  return {
    slot: 1,
    blockTime: null,
    meta: {
      err: opts.err ?? null,
      fee: opts.fee ?? 5_000,
      preBalances: opts.pre,
      postBalances: opts.post,
      preTokenBalances: tok(opts.preTok),
      postTokenBalances: tok(opts.postTok),
    },
    transaction: { signatures: ['sig'], message: { accountKeys: opts.keys.map((pubkey) => ({ pubkey, signer: pubkey === WALLET })) } },
  };
}

describe('parseFillActuals / combineTradeActuals', () => {
  it('splits a buy into spend and refundable ATA rent, and a sell into proceeds', () => {
    // Buy 0.05 SOL: wallet pays 0.05 + 0.0001 fees/tip + 0.00203928 rent for a new ATA.
    const buy = parseFillActuals(
      tx({ keys: [WALLET, ATA, 'Pool'], pre: [1_000_000_000, 0, 0], post: [1_000_000_000 - 50_000_000 - 100_000 - 2_039_280, 2_039_280, 0], fee: 100_000, postTok: '1000' }),
      WALLET, MINT, ATA,
    )!;
    expect(buy.rentLamports).toBe(2_039_280);
    expect(buy.tokenRawDelta).toBe(1000n);
    // Sell: wallet receives 0.06 SOL minus 0.0001 fee.
    const sell = parseFillActuals(
      tx({ keys: [WALLET, ATA], pre: [500_000_000, 2_039_280], post: [500_000_000 + 60_000_000 - 100_000, 2_039_280], fee: 100_000, preTok: '1000', postTok: '0' }),
      WALLET, MINT, ATA,
    )!;
    expect(sell.rentLamports).toBe(0);
    expect(sell.tokenRawDelta).toBe(-1000n);
    const a = combineTradeActuals(buy, [sell])!;
    expect(a.entrySol).toBeCloseTo(0.0501, 9);
    expect(a.exitSol).toBeCloseTo(0.0599, 9);
    expect(a.walletPnlSol).toBeCloseTo(0.0098, 9);
    expect(a.feesSol).toBeCloseTo(0.0002, 9);
    expect(a.rentLockedLamports).toBe(2_039_280);
  });

  it('a failed exit attempt still costs its fee; a never-landed one costs nothing', () => {
    const entry: FillActuals = { signature: 'b', slot: 1, walletLamportsDelta: -50_000_000, feeLamports: 5_000, rentLamports: 0, tokenRawDelta: 1n, err: null };
    const failed: FillActuals = { signature: 'f', slot: 2, walletLamportsDelta: -5_000, feeLamports: 5_000, rentLamports: 0, tokenRawDelta: 0n, err: { Custom: 6004 } };
    const ok: FillActuals = { signature: 's', slot: 3, walletLamportsDelta: 40_000_000, feeLamports: 5_000, rentLamports: 0, tokenRawDelta: -1n, err: null };
    const a = combineTradeActuals(entry, [failed, null, ok])!;
    expect(a.walletPnlSol).toBeCloseTo(-0.010005, 9);
  });
});

function freshRepos() {
  const db = openDb({ path: ':memory:', memory: true });
  return { db, repos: new Repositories(db) };
}
const rowOf = (db: ReturnType<typeof openDb>, mint: string) =>
  db.prepare('select pnl_sol p, net_pnl_sol n, wallet_pnl_sol w, model_pnl_sol m, entry_sol_actual e, exit_sol_actual x from positions where mint = ? order by rowid desc').get(mint) as Record<string, number>;

describe('ActualsRecorder', () => {
  it('overwrites a closed row with wallet-true pnl and keeps the modelled number', async () => {
    const { db, repos } = freshRepos();
    repos.upsertPosition({ mint: 'M', state: 'CLOSED', sizeSol: 0.05, entryPrice: 1, openedAt: 1, closedAt: 2, pnlSol: 0.02 }, { mode: 'live', entryTx: 'buy', netPnlSol: 0.02 });
    const fills: Record<string, FillActuals> = {
      buy: { signature: 'buy', slot: 1, walletLamportsDelta: -52_139_280, feeLamports: 100_000, rentLamports: 2_039_280, tokenRawDelta: 5n, err: null },
      sell: { signature: 'sell', slot: 2, walletLamportsDelta: 30_000_000, feeLamports: 100_000, rentLamports: 0, tokenRawDelta: -5n, err: null },
    };
    const rec = new ActualsRecorder({ repos, executor: { fillActuals: vi.fn(async (sig: string) => fills[sig] ?? null) } });
    const a = await rec.bookTrade({ mint: 'M', entryTx: 'buy', exitTxs: ['sell', 'ghost'] });
    expect(a?.walletPnlSol).toBeCloseTo(-0.0201, 9);
    const row = rowOf(db, 'M');
    expect(row.p).toBeCloseTo(-0.0201, 9);
    expect(row.n).toBeCloseTo(-0.0201, 9);
    expect(row.m).toBeCloseTo(0.02, 9);
    expect(row.e).toBeCloseTo(0.0501, 9);
    // Rent lock recorded once as a wallet event.
    await rec.bookTrade({ mint: 'M', entryTx: 'buy', exitTxs: ['sell'] });
    const rent = db.prepare("select count(*) n, sum(lamports) l from wallet_events where kind = 'rent_lock'").get() as { n: number; l: number };
    expect(rent).toEqual({ n: 1, l: -2_039_280 });
  });

  it('leaves the modelled pnl when the entry tx cannot be read, and never rejects', async () => {
    const { db, repos } = freshRepos();
    repos.upsertPosition({ mint: 'M', state: 'CLOSED', sizeSol: 0.05, entryPrice: 1, openedAt: 1, closedAt: 2, pnlSol: 0.02 }, { mode: 'live', entryTx: 'buy' });
    const rec = new ActualsRecorder({ repos, executor: { fillActuals: vi.fn(async () => { throw new Error('boom'); }) } });
    await expect(rec.bookTrade({ mint: 'M', entryTx: 'buy', exitTxs: [] })).resolves.toBeNull();
    expect(rowOf(db, 'M').p).toBeCloseTo(0.02, 9);
  });
});

describe('mode filter — live desk never sums dry-run rows', () => {
  it('excludes dry-run and simulated rows from live summary and closed pnls', () => {
    const { db, repos } = freshRepos();
    repos.upsertPosition({ mint: 'L', state: 'CLOSED', sizeSol: 0.05, entryPrice: 1, openedAt: 1, closedAt: Date.now(), pnlSol: 0.01 }, { mode: 'live', entryTx: 'b1', netPnlSol: 0.01 });
    repos.upsertPosition({ mint: 'D', state: 'CLOSED', sizeSol: 0.05, entryPrice: 1, openedAt: 1, closedAt: Date.now(), pnlSol: -0.5 }, { mode: 'dry-run', netPnlSol: -0.5, simulated: true });
    repos.upsertPosition({ mint: 'R', state: 'CLOSED', sizeSol: 0.05, entryPrice: 1, openedAt: 1, closedAt: Date.now(), pnlSol: 0 }, { entryTx: 'b2' }); // legacy recovery close
    expect(loadClosedPnls(db, undefined, 'positions', 'live').pnls.sort()).toEqual([0, 0.01]);
    expect(loadClosedPnls(db, undefined, 'positions').pnls).toHaveLength(3);
    const live = ConfigSchema.parse({ mode: 'live', rpc: { primaryHttp: 'https://rpc.example' } });
    expect(getDashboardSummary(db, live).pnl.realizedSol).toBeCloseTo(0.01, 9);
    expect(repos.sumRealizedPnlSince('1970-01-01T00:00:00Z', 'live')).toBeCloseTo(0.01, 9);
  });
});

describe('wallet reconciliation', () => {
  it('explains the wallet change with closed wallet pnl, open cost and rent', () => {
    const { db, repos } = freshRepos();
    repos.recordWalletEvent({ kind: 'balance', lamports: 1_000_000_000 });
    // Make sure later rows sort after the anchor timestamp.
    const later = new Date(Date.now() + 1000).toISOString();
    repos.upsertPosition({ mint: 'A', state: 'CLOSED', sizeSol: 0.05, entryPrice: 1, openedAt: Date.now() + 1000, closedAt: Date.now() + 1000, pnlSol: 0.03 }, { mode: 'live', entryTx: 'b' });
    repos.applyPositionActuals('A', { entrySol: 0.05, exitSol: 0.04, feesSol: 0.0002, walletPnlSol: -0.01 });
    repos.upsertPosition({ mint: 'O', state: 'OPEN', sizeSol: 0.04, entryPrice: 1, openedAt: Date.now() + 1000 }, { mode: 'live', entryTx: 'b2' });
    db.prepare("insert into wallet_events (kind, lamports, created_at) values ('rent_lock', -2039280, ?)").run(later);
    db.prepare("insert into wallet_events (kind, lamports, created_at) values ('balance', ?, ?)").run(1_000_000_000 - 10_000_000 - 40_000_000 - 2_039_280, later);
    const r = computeWalletReconciliation(repos);
    expect(r.available).toBe(true);
    expect(r.walletDeltaSol).toBeCloseTo(-0.05203928, 9);
    expect(r.closedPnlSol).toBeCloseTo(-0.01, 9);
    expect(r.closedModelPnlSol).toBeCloseTo(0.03, 9);
    expect(r.openCostSol).toBeCloseTo(0.04, 9);
    expect(r.unexplainedSol).toBeCloseTo(0, 9);
  });

  it('is unavailable before the first balance snapshot', () => {
    const { repos } = freshRepos();
    expect(computeWalletReconciliation(repos).available).toBe(false);
  });
});
