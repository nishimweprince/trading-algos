import { describe, expect, it } from 'vitest';
import { ConfigSchema } from '../src/config/schema.ts';
import { openDb } from '../src/persistence/db.ts';
import { Repositories } from '../src/persistence/repositories.ts';
import { scoreCandidate } from '../src/guardrails/scoring.ts';
import {
  buildStrategyWeekReport,
  listStrategyTrades,
  renderStrategyWeekMarkdown,
  strategyTradesCsv,
} from '../src/dashboard/strategyReport.ts';
import { createDashboardApp } from '../src/dashboard/server.ts';
import type { Candidate } from '../src/enrichment/types.ts';

describe('strategy week report', () => {
  it('builds JSON + markdown with strata and underpowered flags for small n', () => {
    const db = openDb({ path: ':memory:', memory: true });
    const config = ConfigSchema.parse({
      mode: 'live',
      rpc: { primaryHttp: 'http://localhost:8899' },
      jito: { blockEngineUrl: 'http://localhost:8080' },
    });
    const repos = new Repositories(db);
    repos.startRunSession({
      mode: 'live',
      configHash: 'abc123',
      configJson: JSON.stringify({ mode: 'live', entry: { minEntryScore: 60 } }),
      gitCommit: 'deadbeef',
    });

    seedClosed(db, 'mintA', 0.02, 'TAKE_PROFIT_1', 80, false);
    seedClosed(db, 'mintB', -0.01, 'STOP_LOSS', 65, false);
    seedClosed(db, 'mintC', -0.015, 'TRAILING_STOP', 70, true, 30);
    seedClosed(db, 'mintD', 0.005, 'TIME_STOP', 62, false);

    db.prepare(
      `INSERT INTO position_fills (mint, trigger, fraction, gain_pct, pnl_sol)
       VALUES ('mintA', 'TAKE_PROFIT_1', 0.75, 50, 0.01)`,
    ).run();
    db.prepare(
      `INSERT INTO position_fills (mint, trigger, fraction, gain_pct, pnl_sol)
       VALUES ('mintA', 'TRAILING_STOP', 0.25, 20, -0.005)`,
    ).run();

    const report = buildStrategyWeekReport(db, config, { range: 'all', mode: 'live' });
    expect(report.schemaVersion).toBe(1);
    expect(report.headline.tradeCount).toBe(4);
    expect(report.strata.byExitReason.length).toBeGreaterThan(0);
    expect(report.strata.byScoreBucket.some((s) => s.underpowered)).toBe(true);
    expect(report.configSessions[0]?.configHash).toBe('abc123');
    expect(report.codeMap.scoring).toContain('scoring.ts');

    const md = renderStrategyWeekMarkdown(report);
    expect(md).toContain('Strategy Week Review');
    expect(md).toContain('Hypotheses');
    expect(md).toContain('Code map');

    const trades = listStrategyTrades(db, 'all', 'live');
    expect(trades).toHaveLength(4);
    expect(strategyTradesCsv(trades)).toContain('mintA');
    db.close();
  });

  it('scoreCandidate exposes components', () => {
    const c = minimalCandidate();
    const soft = scoreCandidate(c);
    expect(soft.components.baseline).toBe(40);
    expect(soft.components.authorities).toBe(15);
    expect(soft.score).toBeGreaterThan(40);
  });

  it('serves strategy-week endpoints', async () => {
    const db = openDb({ path: ':memory:', memory: true });
    seedClosed(db, 'm1', 0.01, 'TAKE_PROFIT_2', 85, false);
    const app = createDashboardApp({
      config: ConfigSchema.parse({
        mode: 'live',
        rpc: { primaryHttp: 'http://localhost:8899' },
        jito: { blockEngineUrl: 'http://localhost:8080' },
        dashboard: { enabled: true, host: '127.0.0.1', port: 8787 },
      }),
      db,
    });
    expect((await app.request('/api/reports/strategy-week.json?range=all')).status).toBe(200);
    const md = await app.request('/api/reports/strategy-week.md?range=all');
    expect(md.status).toBe(200);
    expect(md.headers.get('content-type')).toContain('markdown');
    db.close();
  });
});

function seedClosed(
  db: ReturnType<typeof openDb>,
  mint: string,
  net: number,
  exit: string,
  score: number,
  hv: boolean,
  leftOnTable = 5,
): void {
  db.prepare(
    `INSERT INTO positions (
       mint, entry_price, size_sol, state, pnl_sol, net_pnl_sol, fees_sol, pnl_pct, exit_reason,
       entry_soft_score, high_volatility, mfe_pct, mae_pct, left_on_table_pct, hold_ms,
       mode, opened_at, closed_at, momentum_window_ms, detect_to_open_ms
     ) VALUES (
       ?, 1, 0.03, 'CLOSED', ?, ?, 0.001, ?, ?,
       ?, ?, ?, -5, ?, 8000,
       'live', datetime('now'), datetime('now'), 500, 1200
     )`,
  ).run(mint, net, net, (net / 0.03) * 100, exit, score, hv ? 1 : 0, leftOnTable + (net / 0.03) * 100, leftOnTable);
}

function minimalCandidate(): Candidate {
  return {
    graduation: {
      mint: 'M',
      venue: 'pumpswap',
      poolAddress: 'P',
      slot: 1,
      feedSource: 'pumpportal',
      receivedAtNs: 1n,
    },
    enrichment: {
      mintInfo: {
        decimals: 6,
        supply: 1_000_000n,
        mintAuthority: null,
        freezeAuthority: null,
        isToken2022: false,
        extensions: [],
      },
      metadata: { hasSocials: true, name: 'x', symbol: 'X' },
      unknowns: [],
      elapsedMs: 10,
    },
  };
}
