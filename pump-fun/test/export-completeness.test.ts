import { describe, it, expect } from 'vitest';
import { PositionManager } from '../src/positions/manager.ts';
import type { PricePoller, PriceTick, PoolRef } from '../src/positions/pricing.ts';
import { TypedBus } from '../src/core/bus.ts';
import { openDb } from '../src/persistence/db.ts';
import { Repositories } from '../src/persistence/repositories.ts';
import { ConfigSchema } from '../src/config/schema.ts';
import { buildTradeBlotterCsv } from '../src/dashboard/queries.ts';
import { marketCapSol } from '../src/dashboard/features.ts';
import type { PoolPricingRef } from '../src/core/types.ts';
import type { EnrichmentData } from '../src/enrichment/types.ts';

/** Work plan 2026-09-25 P0.5 / F15: export completeness. */

class FakePoller {
  handler: (t: PriceTick) => void = () => {};
  setHandler(h: (t: PriceTick) => void) { this.handler = h; }
  register(_r: PoolRef) {}
  unregister(_m: string) {}
  async readOnce() { return null; }
  start() {}
  stop() {}
  tick(mint: string, price: number, atMs: number) {
    this.handler({ mint, price, baseReserve: 0n, quoteReserveLamports: 0n, atMs });
  }
}

const pricing = (): PoolPricingRef => ({
  poolAddress: 'pool',
  baseMint: 'mint',
  baseVault: 'b',
  quoteVault: 'q',
  baseDecimals: 6,
  baseReserve: 10n ** 15n,
  quoteReserveLamports: 100n * 10n ** 9n,
});

const flush = () => new Promise((resolve) => setTimeout(resolve, 0));

function parseCsv(csv: string): Array<Record<string, string>> {
  const [head, ...rows] = csv.trim().split('\n');
  const headers = head!.split(',');
  // Test rows carry no embedded commas in the asserted columns.
  return rows.map((r) => Object.fromEntries(r.split(',').map((v, i) => [headers[i]!, v])));
}

describe('trade export completeness (F15)', () => {
  it('takes feed_source / venue / detect->open from the openPosition event, not the late graduations row', async () => {
    const bus = new TypedBus();
    const db = openDb({ path: ':memory:', memory: true });
    const repos = new Repositories(db);
    const config = ConfigSchema.parse({ mode: 'paper' });
    const poller = new FakePoller();
    let now = 10_000;
    const mgr = new PositionManager({ config, bus, repos, poller: poller as unknown as PricePoller, now: () => now });
    mgr.start();

    // No graduations row exists yet — exactly the race that left 411/524 rows NULL.
    bus.emit('openPosition', {
      mint: 'M1',
      sizeSol: 0.05,
      highVolatility: false,
      pricing: pricing(),
      feedSource: 'laserstream',
      venue: 'pumpswap',
      entrySoftScore: 85,
      detectedAtMs: 8_700,
    });
    await flush();
    now = 11_000;
    poller.tick('M1', 0.8e-7, now); // -20% -> stop
    mgr.stop?.();

    const rows = parseCsv(buildTradeBlotterCsv(db, { range: 'all' }));
    expect(rows).toHaveLength(1);
    const r = rows[0]!;
    expect(r.feed_source).toBe('laserstream');
    expect(r.venue).toBe('pumpswap');
    expect(r.entry_soft_score).toBe('85');
    expect(Number(r.entry_detect_to_open_ms)).toBe(1_300);
  });

  it('carries candidate features (mint age, creator, mcap, H4) through to the export', async () => {
    const bus = new TypedBus();
    const db = openDb({ path: ':memory:', memory: true });
    const repos = new Repositories(db);
    const config = ConfigSchema.parse({ mode: 'paper' });
    const poller = new FakePoller();
    const mgr = new PositionManager({ config, bus, repos, poller: poller as unknown as PricePoller, now: () => 0 });
    mgr.start();

    repos.recordVerdict(
      {
        mint: 'M2',
        verdict: 'accept',
        hardChecks: [
          { id: 'H4', label: 'sellable', status: 'unknown', reason: 'price_moved' },
          { id: 'H12', label: 'population', status: 'pass' },
        ],
        softScore: 85,
        vetoReasons: [],
        highVolatility: false,
        sizeMultiplier: 1,
      },
      null,
      {
        earlyFlowNetSol: 1.5,
        poolSolAtEntry: 84,
        mintAgeMs: 45_000,
        creator: 'Creator111',
        mcapSolAtEntry: 380,
        sellabilityStatus: 'unknown',
        sellabilityReason: 'price_moved',
        poolMovePct: 17.5,
        populationOk: true,
      },
    );
    bus.emit('openPosition', { mint: 'M2', sizeSol: 0.05, highVolatility: false, pricing: pricing() });
    await flush();
    poller.tick('M2', 0.8e-7, 500);

    const r = parseCsv(buildTradeBlotterCsv(db, { range: 'all' }))[0]!;
    expect(r.early_flow_sol).toBe('1.5');
    expect(r.pool_sol_at_entry).toBe('84');
    expect(r.mint_age_ms).toBe('45000');
    expect(r.creator).toBe('Creator111');
    expect(r.mcap_sol_at_entry).toBe('380');
    expect(r.sellability_status).toBe('unknown');
    expect(r.sellability_reason).toBe('price_moved');
    expect(r.pool_move_pct).toBe('17.5');
    expect(r.population_ok).toBe('1');
  });

  it('dry track emits the same headers as live', () => {
    const db = openDb({ path: ':memory:', memory: true });
    const live = buildTradeBlotterCsv(db, { range: 'all' }).split('\n')[0]!;
    const dry = buildTradeBlotterCsv(db, { range: 'all', track: 'dry' }).split('\n')[0]!;
    expect(dry).toBe(`${live},live_status`);
  });

  it('computes FDV market cap in SOL from reserves and supply', () => {
    // 1e9 whole tokens (6 dp) supply, pool holds 200M tokens against 80 SOL
    // -> price 4e-7 SOL/token -> mcap 400 SOL.
    const e = {
      pool: { baseReserve: 200_000_000n * 10n ** 6n, quoteReserveLamports: 80n * 10n ** 9n },
      mintInfo: { supply: 1_000_000_000n * 10n ** 6n },
      unknowns: [],
      elapsedMs: 0,
    } as unknown as EnrichmentData;
    expect(marketCapSol(e)).toBeCloseTo(400, 9);
  });
});
