import { mkdtempSync, readFileSync, rmSync } from 'fs';
import { tmpdir } from 'os';
import { join } from 'path';
import { BrowserService } from '../src/browser/browser.service';
import { AppConfigService } from '../src/config/app-config.service';
import { DedupService } from '../src/dedup/dedup.service';
import { JsonlLoggerService } from '../src/logging/jsonl-logger.service';
import { TradingIdea } from '../src/models/trading-idea.model';
import { Mt5ExecutionService } from '../src/mt5/mt5-execution.service';
import { AutochartistExtractor } from '../src/scraper/extractors/autochartist.extractor';
import { TradingCentralExtractor } from '../src/scraper/extractors/trading-central.extractor';
import { ScraperService } from '../src/scraper/scraper.service';

const fixture = JSON.parse(
  readFileSync(join(__dirname, 'fixtures', 'trading-central-api.json'), 'utf8'),
);

function response(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

function build(dir: string, enabled: boolean) {
  const config = new AppConfigService();
  config.load({
    SOURCES: JSON.stringify([
      { type: 'TRADING_CENTRAL', url: 'https://example.com/tc' },
    ]),
    IDEAS_LOG_PATH: join(dir, 'ideas.jsonl'),
    SEEN_STATE_PATH: join(dir, 'seen.json'),
    SCREENSHOT_DIR: join(dir, 'screenshots'),
    MT5_SIGNAL_TRADING_ENABLED: String(enabled),
    MT5_SIGNAL_API_KEY: enabled ? 'test-api-key-value' : '',
    MT5_SIGNAL_RULES: JSON.stringify({
      'USD/CAD': { symbol: 'USDCAD', volume: '0.05' },
      'EUR/USD': { symbol: 'EURUSD', volume: '0.10' },
    }),
  });
  const browser = new BrowserService(config);
  const dedup = new DedupService(config);
  dedup.onModuleInit();
  const mt5 = new Mt5ExecutionService(config, dedup);
  const jsonl = new JsonlLoggerService(config);
  const scraper = new ScraperService(
    config,
    browser,
    dedup,
    jsonl,
    new TradingCentralExtractor(),
    new AutochartistExtractor(),
    mt5,
  );
  return { config, dedup, mt5, jsonl, scraper };
}

describe('MT5 scraper orchestration', () => {
  let dir: string;

  beforeEach(() => {
    dir = mkdtempSync(join(tmpdir(), 'mt5-orchestration-'));
  });

  afterEach(() => {
    rmSync(dir, { recursive: true, force: true });
  });

  it('queues and submits each new eligible Trading Central idea only once', async () => {
    const { scraper, mt5, dedup, jsonl } = build(dir, true);
    const posts: Array<{ url: string; request: Record<string, unknown> }> = [];
    mt5.setFetchImplementation(async (input, init) => {
      const url = String(input);
      if (url.endsWith('/health/ready')) {
        return response({ status: 'ready' });
      }
      const request = JSON.parse(String(init?.body)) as Record<string, unknown>;
      posts.push({ url, request });
      return response({ signal_id: request.signal_id, outcome: 'filled' });
    });
    scraper.setHooks({
      openPage: async () => null,
      waitForContent: async () => undefined,
      isLoginWall: async () => false,
      takeScreenshot: async () => join(dir, 'tc.png'),
      networkPayloads: () => [fixture],
    });

    const first = await scraper.runAllSources();
    const second = await scraper.runAllSources();

    expect(first.totalNew).toBe(3);
    expect(second.totalNew).toBe(0);
    expect(posts).toHaveLength(2);
    expect(posts.map((value) => value.request.symbol).sort()).toEqual([
      'EURUSD',
      'USDCAD',
    ]);
    expect(posts.every((value) => value.request.execution_type === 'market')).toBe(
      true,
    );
    expect(jsonl.readAll()).toHaveLength(3);
    expect(Object.values(dedup.getStore().allExecutions())).toHaveLength(3);
    expect(
      Object.values(dedup.getStore().allExecutions()).filter(
        (record) => record.status === 'succeeded',
      ),
    ).toHaveLength(2);
    expect(
      Object.values(dedup.getStore().allExecutions()).filter(
        (record) => record.status === 'skipped',
      ),
    ).toHaveLength(1);
  });

  it('does not create a backlog or make API calls while disabled', async () => {
    const { scraper, mt5, dedup } = build(dir, false);
    const fetchMock = jest.fn();
    mt5.setFetchImplementation(fetchMock);
    scraper.setHooks({
      openPage: async () => null,
      waitForContent: async () => undefined,
      isLoginWall: async () => false,
      takeScreenshot: async () => join(dir, 'tc.png'),
      networkPayloads: () => [fixture],
    });

    await scraper.runAllSources();

    expect(fetchMock).not.toHaveBeenCalled();
    expect(dedup.getStore().allExecutions()).toEqual({});
    expect(dedup.getStore().size()).toBe(3);
  });

  it('drains an existing outbox even when the current scrape fails', async () => {
    const { scraper, mt5, dedup } = build(dir, true);
    const pendingIdea: TradingIdea = {
      provider: 'TRADING_CENTRAL',
      instrument: 'EUR/USD',
      timeframe: '30 MIN',
      direction: 'UP',
      stopLoss: 1.08,
      takeProfit: 1.1,
      ideaTimestamp: null,
      capturedAt: new Date().toISOString(),
      sourceUrl: 'https://example.com/tc',
      hash: 'f'.repeat(64),
    };
    dedup.markSeen(
      [pendingIdea],
      mt5.createExecutionRecords([pendingIdea]),
    );
    const post = jest.fn();
    mt5.setFetchImplementation(async (input, init) => {
      if (String(input).endsWith('/health/ready')) {
        return response({ status: 'ready' });
      }
      post();
      const request = JSON.parse(String(init?.body)) as Record<string, unknown>;
      return response({ signal_id: request.signal_id, outcome: 'filled' });
    });
    scraper.setHooks({
      openPage: async () => {
        throw new Error('browser unavailable');
      },
      networkPayloads: () => [fixture],
    });

    const result = await scraper.runAllSources();

    expect(result.results[0].ok).toBe(false);
    expect(post).toHaveBeenCalledTimes(1);
    expect(dedup.listExecutions()[0].status).toBe('succeeded');
  });
});
