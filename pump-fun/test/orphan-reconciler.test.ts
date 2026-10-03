import { describe, expect, it, vi } from 'vitest';
import { OrphanReconciler, type OrphanExecutor } from '../src/positions/orphanReconciler.ts';
import { TypedBus } from '../src/core/bus.ts';
import { Repositories } from '../src/persistence/repositories.ts';
import { openDb } from '../src/persistence/db.ts';
import { ConfigSchema } from '../src/config/schema.ts';
import { PROGRAM_IDS, WSOL_MINT } from '../src/core/constants.ts';
import type { BroadcastResult } from '../src/executor/broadcaster.ts';

const cfg = ConfigSchema.parse({
  mode: 'live',
  rpc: { primaryHttp: 'https://rpc.example' },
  exits: { ladderSlippageTiers: [5, 25], emergencySlippagePct: 90, maxExitAttempts: 3 },
  execution: { orphanIgnoreMints: ['KEEP'] },
});

const acct = (mint: string, amount: bigint, programId: string = PROGRAM_IDS.TOKEN) => ({
  address: `ata-${mint}`, mint, programId, lamports: 2_039_280, amount,
});
const ok = (sig: string): BroadcastResult => ({ mode: 'live', simulated: true, sent: true, confirmed: true, signature: sig, attempts: [] });

function setup(over: Partial<OrphanExecutor> = {}, tracked: string[] = []) {
  const bus = new TypedBus();
  const repos = new Repositories(openDb({ path: ':memory:', memory: true }));
  const balances = new Map<string, bigint>([['ORPH', 1_000n], ['OPEN', 5n], ['KEEP', 9n], [WSOL_MINT, 3n], ['EMPTY', 0n]]);
  let sol = 100_000_000;
  const executor: OrphanExecutor = {
    listTokenAccounts: vi.fn(async () => [...balances].map(([m, a]) => acct(m, a))),
    canonicalPoolFor: vi.fn((m: string) => `canon-${m}`),
    estimateSellLamports: vi.fn(async () => 10_000_000n),
    sellAndConfirm: vi.fn(async (_pool: string, mint: string) => {
      balances.set(mint, 0n);
      sol += 9_000_000;
      return ok(`sell-${mint}`);
    }),
    readTokenBalance: vi.fn(async (mint: string) => balances.get(mint) ?? 0n),
    solBalanceLamports: vi.fn(async () => sol),
    ...over,
  };
  const rec = new OrphanReconciler({ config: cfg, bus, repos, executor, trackedMints: () => tracked });
  const alerts: string[] = [];
  const kills: string[] = [];
  bus.on('alert', (a) => alerts.push(a.message));
  bus.on('killSwitch', (k) => kills.push(k.detail ?? ''));
  return { bus, repos, executor, rec, balances, alerts, kills };
}

describe('OrphanReconciler', () => {
  it('sells only untracked non-zero balances, skipping tracked, WSOL, ignored and empty accounts', async () => {
    const { rec, executor } = setup({}, ['OPEN']);
    const r = await rec.sweep();
    expect(r.found).toBe(1);
    expect(r.sold).toEqual(['ORPH']);
    expect(executor.sellAndConfirm).toHaveBeenCalledTimes(1);
    expect(executor.sellAndConfirm).toHaveBeenCalledWith('canon-ORPH', 'ORPH', 1_000n, 5);
  });

  it('sells through the pool on the mint\'s last FAILED row and books the unbooked cost', async () => {
    const { rec, repos, executor } = setup({}, ['OPEN']);
    repos.upsertPosition(
      { mint: 'ORPH', state: 'FAILED', sizeSol: 0.05, entryPrice: 1e-7, openedAt: Date.now() },
      { entryTx: 'buy-sig', pricingJson: JSON.stringify({ poolAddress: 'real-pool', baseMint: 'ORPH' }) },
    );
    await rec.sweep();
    expect(executor.sellAndConfirm).toHaveBeenCalledWith('real-pool', 'ORPH', 1_000n, 5);
    const db = (repos as unknown as { db: { prepare(s: string): { get(...a: unknown[]): unknown } } }).db;
    const row = db.prepare("select state, exit_reason r, pnl_sol p, entry_tx e, exit_tx x, mode from positions where mint = 'ORPH' order by rowid desc").get() as Record<string, unknown>;
    expect(row).toMatchObject({ state: 'CLOSED', r: 'ORPHAN_RECOVERY', e: 'buy-sig', x: 'sell-ORPH', mode: 'live' });
    expect(row.p as number).toBeCloseTo(0.009 - 0.05, 9);
  });

  it('escalates slippage across attempts and engages the kill switch once when a sell is stuck', async () => {
    const sellAndConfirm = vi.fn(async (_pool: string, _mint: string, _raw: bigint, _slip: number): Promise<BroadcastResult> => ({ mode: 'live', simulated: true, sent: false, confirmed: false, simErr: 'slippage', attempts: [] }));
    const { rec, kills, alerts } = setup({ sellAndConfirm }, ['OPEN']);
    const r = await rec.sweep();
    expect(r.stuck).toEqual(['ORPH']);
    expect(sellAndConfirm.mock.calls.map((c) => c[3])).toEqual([5, 25, 90]);
    expect(kills).toHaveLength(1);
    expect(alerts.some((a) => a.includes('CRITICAL orphan'))).toBe(true);
    // Backed off: the next sweep does not hammer it again.
    await rec.sweep();
    expect(sellAndConfirm).toHaveBeenCalledTimes(3);
  });

  it('leaves dust below orphanMinProceedsSol and alerts once', async () => {
    const { rec, executor, alerts } = setup({ estimateSellLamports: vi.fn(async () => 1_000n) }, ['OPEN']);
    expect((await rec.sweep()).dust).toEqual(['ORPH']);
    await rec.sweep();
    expect(executor.sellAndConfirm).not.toHaveBeenCalled();
    expect(alerts.filter((a) => a.includes('dust'))).toHaveLength(1);
  });

  it('ignores a token with no PumpSwap pool and no trade history (warn once, no kill switch)', async () => {
    const { rec, executor, kills, alerts } = setup({ estimateSellLamports: vi.fn(async () => { throw new Error('Pool account not found'); }) }, ['OPEN']);
    await rec.sweep();
    await rec.sweep();
    expect(executor.estimateSellLamports).toHaveBeenCalledTimes(1);
    expect(executor.sellAndConfirm).not.toHaveBeenCalled();
    expect(kills).toHaveLength(0);
    expect(alerts.filter((a) => a.includes('ignoring'))).toHaveLength(1);
  });

  it('never sells a mint the curve lane still holds', async () => {
    const { rec, repos, executor } = setup({}, ['OPEN']);
    repos.recordCurvePosition({ mint: 'ORPH', state: 'OPEN', sizeSol: 0.01 } as never);
    expect((await rec.sweep()).found).toBe(0);
    expect(executor.sellAndConfirm).not.toHaveBeenCalled();
  });

  it('does nothing outside live mode', async () => {
    const bus = new TypedBus();
    const repos = new Repositories(openDb({ path: ':memory:', memory: true }));
    const listTokenAccounts = vi.fn(async () => []);
    const rec = new OrphanReconciler({
      config: ConfigSchema.parse({ mode: 'dry-run', rpc: { primaryHttp: 'https://rpc.example' } }),
      bus, repos, executor: { listTokenAccounts } as unknown as OrphanExecutor, trackedMints: () => [],
    });
    await rec.start();
    expect(listTokenAccounts).not.toHaveBeenCalled();
  });
});
