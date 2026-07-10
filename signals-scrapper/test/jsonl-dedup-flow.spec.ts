import { mkdtempSync, rmSync } from 'fs';
import { tmpdir } from 'os';
import { join } from 'path';
import { AppConfigService } from '../src/config/app-config.service';
import { computeIdeaHash } from '../src/dedup/hash';
import { DedupService } from '../src/dedup/dedup.service';
import { JsonlLoggerService } from '../src/logging/jsonl-logger.service';
import { TradingIdea } from '../src/models/trading-idea.model';

function makeConfig(dir: string): AppConfigService {
  const cfg = new AppConfigService();
  cfg.load({
    SOURCES: JSON.stringify([
      {
        type: 'TRADING_CENTRAL',
        url: 'https://secure.icmarkets.com/TradingCentral/TradingCentral',
      },
    ]),
    IDEAS_LOG_PATH: join(dir, 'ideas.jsonl'),
    SEEN_STATE_PATH: join(dir, 'seen.json'),
    SCREENSHOT_DIR: join(dir, 'screenshots'),
    BROWSER_MODE: 'CDP',
    CRON_EXPRESSION: '*/15 * * * *',
  });
  return cfg;
}

function idea(partial: Partial<TradingIdea> & Pick<TradingIdea, 'instrument' | 'target' | 'direction'>): TradingIdea {
  const base = {
    provider: 'TRADING_CENTRAL' as const,
    timeframe: '30 MIN',
    ideaTimestamp: '2026-07-10T17:10:00.000Z',
    capturedAt: '2026-07-10T17:20:00.000Z',
    sourceUrl: 'https://example.com',
    ...partial,
  };
  return { ...base, hash: computeIdeaHash(base) };
}

describe('JSONL logger + dedup flow', () => {
  let dir: string;

  beforeEach(() => {
    dir = mkdtempSync(join(tmpdir(), 'jsonl-dedup-'));
  });

  afterEach(() => {
    rmSync(dir, { recursive: true, force: true });
  });

  it('appends only new ideas; second run with same ideas writes zero lines', () => {
    const config = makeConfig(dir);
    const dedup = new DedupService(config);
    dedup.onModuleInit();
    const jsonl = new JsonlLoggerService(config);

    const batch = [
      idea({ instrument: 'USD/CAD', direction: 'DOWN', target: 1.4115 }),
      idea({ instrument: 'EUR/USD', direction: 'UP', target: 1.085 }),
    ];

    const firstNew = dedup.filterNew(batch);
    expect(firstNew).toHaveLength(2);
    expect(jsonl.appendIdeas(firstNew)).toBe(2);
    dedup.markSeen(firstNew);

    const logged1 = jsonl.readAll();
    expect(logged1).toHaveLength(2);
    expect(logged1.map((i) => i.instrument).sort()).toEqual([
      'EUR/USD',
      'USD/CAD',
    ]);

    // Second run — same ideas
    const secondNew = dedup.filterNew(batch);
    expect(secondNew).toHaveLength(0);
    expect(jsonl.appendIdeas(secondNew)).toBe(0);
    expect(jsonl.readAll()).toHaveLength(2);
  });

  it('changed target produces a new JSONL line', () => {
    const config = makeConfig(dir);
    const dedup = new DedupService(config);
    dedup.onModuleInit();
    const jsonl = new JsonlLoggerService(config);

    const v1 = idea({ instrument: 'USD/CAD', direction: 'DOWN', target: 1.4115 });
    const new1 = dedup.filterNew([v1]);
    jsonl.appendIdeas(new1);
    dedup.markSeen(new1);

    const v2 = idea({ instrument: 'USD/CAD', direction: 'DOWN', target: 1.41 });
    expect(v2.hash).not.toBe(v1.hash);
    const new2 = dedup.filterNew([v2]);
    expect(new2).toHaveLength(1);
    jsonl.appendIdeas(new2);
    dedup.markSeen(new2);

    const all = jsonl.readAll();
    expect(all).toHaveLength(2);
    expect(all[1].target).toBe(1.41);
  });

  it('restart does not re-log old ideas (seen state persisted)', () => {
    const config = makeConfig(dir);
    const dedup1 = new DedupService(config);
    dedup1.onModuleInit();
    const jsonl = new JsonlLoggerService(config);

    const batch = [
      idea({ instrument: 'GBP/USD', direction: 'UP', target: 1.3 }),
    ];
    const n = dedup1.filterNew(batch);
    jsonl.appendIdeas(n);
    dedup1.markSeen(n);
    dedup1.flush();

    // Simulate process restart
    const dedup2 = new DedupService(config);
    dedup2.onModuleInit();
    const again = dedup2.filterNew(batch);
    expect(again).toHaveLength(0);
    expect(jsonl.readAll()).toHaveLength(1);
  });
});
