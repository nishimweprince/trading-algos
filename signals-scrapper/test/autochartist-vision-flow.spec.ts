import { mkdtempSync, readFileSync, rmSync } from 'fs';
import { tmpdir } from 'os';
import { join } from 'path';
import { Page } from 'playwright';
import { BrowserService } from '../src/browser/browser.service';
import { AppConfigService } from '../src/config/app-config.service';
import { DedupService } from '../src/dedup/dedup.service';
import { computeIdeaHash } from '../src/dedup/hash';
import { JsonlLoggerService } from '../src/logging/jsonl-logger.service';
import { Mt5ExecutionService } from '../src/mt5/mt5-execution.service';
import { OpenAiVisionService } from '../src/openai/openai-vision.service';
import { AutochartistExtractor } from '../src/scraper/extractors/autochartist.extractor';
import { TradingCentralExtractor } from '../src/scraper/extractors/trading-central.extractor';
import { ScraperService } from '../src/scraper/scraper.service';

const sourceUrl = 'https://secure.ic.com/AutoChartist/SignIn';

function response(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

describe('Autochartist OpenAI vision orchestration', () => {
  let dir: string;

  beforeEach(() => {
    dir = mkdtempSync(join(tmpdir(), 'ac-vision-flow-'));
  });

  afterEach(() => {
    rmSync(dir, { recursive: true, force: true });
  });

  function buildVision(): OpenAiVisionService {
    return {
      extractAutochartist: jest.fn().mockImplementation((_path, ctx) => {
        const base = {
          provider: 'AUTOCHARTIST' as const,
          instrument: 'USD/ZAR',
          timeframe: '15',
          direction: 'UP' as const,
          entry: 16.5,
          stopLoss: 16.4, // risk-reward mirror of target 16.6 across entry 16.5
          pivot: 16.4,
          takeProfit: 16.6,
          target: 16.6,
          ideaTimestamp: '2026-07-21T15:00:00.000Z',
          capturedAt: ctx.capturedAt,
          sourceUrl,
          screenshotPath: ctx.screenshotPath,
        };
        return Promise.resolve({
          ideas: [{ ...base, hash: computeIdeaHash(base) }],
          rejected: [],
          model: 'gpt-5.6-luna',
          rawResponse: '{"signals":[]}',
        });
      }),
    } as unknown as OpenAiVisionService;
  }

  function build(enabled: boolean, vision: OpenAiVisionService) {
    const config = new AppConfigService();
    config.load({
      SOURCES: JSON.stringify([{ type: 'AUTOCHARTIST', url: sourceUrl }]),
      BROWSER_MODE: 'CDP',
      OPENAI_API_KEY: 'test-key',
      IDEAS_LOG_PATH: join(dir, 'ideas.jsonl'),
      SEEN_STATE_PATH: join(dir, 'seen.json'),
      SCREENSHOT_DIR: join(dir, 'screenshots'),
      MT5_SIGNAL_TRADING_ENABLED: String(enabled),
      MT5_SIGNAL_API_KEY: enabled ? 'test-api-key-value' : '',
      MT5_SIGNAL_RULES: JSON.stringify({
        'USD/ZAR': { symbol: 'USDZAR', volume: '0.05' },
      }),
    });
    const browser = new BrowserService(config);
    const dedup = new DedupService(config);
    dedup.onModuleInit();
    const jsonl = new JsonlLoggerService(config);
    const mt5 = new Mt5ExecutionService(config, dedup);
    const scraper = new ScraperService(
      config,
      browser,
      dedup,
      jsonl,
      new TradingCentralExtractor(),
      new AutochartistExtractor(vision, config),
      mt5,
    );
    return { config, dedup, jsonl, mt5, scraper };
  }

  function hooks(scraper: ScraperService) {
    const page = { isClosed: () => false } as unknown as Page;
    scraper.setHooks({
      createPage: async () => page,
      navigate: async () => undefined,
      waitForContent: async () => undefined,
      prepareView: async () => undefined,
      isLoginWall: async () => false,
      takeScreenshot: async () => join(dir, 'ac.png'),
      closePage: async () => undefined,
    });
  }

  it('writes JSONL once, dedups on re-run, and records a success debug run', async () => {
    const vision = buildVision();
    const { scraper, jsonl } = build(false, vision);
    hooks(scraper);

    const first = await scraper.runAllSources();
    const second = await scraper.runAllSources();

    expect(first.totalNew).toBe(1);
    expect(second.totalNew).toBe(0);
    expect(jsonl.readAll()).toHaveLength(1);
    expect(jsonl.readAll()[0].provider).toBe('AUTOCHARTIST');
    expect(vision.extractAutochartist).toHaveBeenCalledTimes(2);

    const seen = JSON.parse(readFileSync(join(dir, 'seen.json'), 'utf8'));
    expect(seen.runs).toHaveLength(2);
    expect(seen.runs.every((run: { status: string }) => run.status === 'success')).toBe(
      true,
    );
    expect(seen.runs[0].openai).toMatchObject({ model: 'gpt-5.6-luna' });
  });

  it('submits the mirrored SL/TP as an MT5 market order when trading is enabled', async () => {
    const vision = buildVision();
    const { scraper, mt5 } = build(true, vision);
    const posts: Array<Record<string, unknown>> = [];
    mt5.setFetchImplementation(async (input, init) => {
      const url = String(input);
      if (url.endsWith('/health/ready')) {
        return response({ status: 'ready' });
      }
      if (url.includes('/v1/market-data/tick')) {
        return response({ symbol: 'USDZAR', bid: 16.58, ask: 16.59 });
      }
      const request = JSON.parse(String(init?.body)) as Record<string, unknown>;
      posts.push(request);
      return response({ signal_id: request.signal_id, outcome: 'filled' });
    });
    hooks(scraper);

    await scraper.runAllSources();

    expect(posts).toHaveLength(1);
    expect(posts[0]).toMatchObject({
      execution_type: 'market',
      symbol: 'USDZAR',
      direction: 'buy',
      volume: '0.05',
      stop_loss: String(2 * 16.59 - 16.6),
      take_profit: '16.6',
    });
  });
});
