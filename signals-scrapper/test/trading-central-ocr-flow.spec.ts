import { mkdtempSync, readFileSync, rmSync } from 'fs';
import { tmpdir } from 'os';
import { join } from 'path';
import { Page } from 'playwright';
import { AppConfigService } from '../src/config/app-config.service';
import {
  BrowserAccessError,
  BrowserService,
} from '../src/browser/browser.service';
import { DedupService } from '../src/dedup/dedup.service';
import { computeIdeaHash } from '../src/dedup/hash';
import { JsonlLoggerService } from '../src/logging/jsonl-logger.service';
import { OcrService } from '../src/ocr/ocr.service';
import { OllamaFormatterService } from '../src/ollama/ollama-formatter.service';
import { AutochartistExtractor } from '../src/scraper/extractors/autochartist.extractor';
import { TradingCentralExtractor } from '../src/scraper/extractors/trading-central.extractor';
import { ScraperService } from '../src/scraper/scraper.service';

describe('Trading Central screenshot OCR orchestration', () => {
  let dir: string;

  beforeEach(() => {
    dir = mkdtempSync(join(tmpdir(), 'tc-ocr-flow-'));
  });

  afterEach(() => {
    rmSync(dir, { recursive: true, force: true });
  });

  it('writes JSONL once and keeps two successful debug runs', async () => {
    const sourceUrl =
      'https://secure.ic.com/TradingCentral/TradingCentral';
    const config = new AppConfigService();
    config.load({
      SOURCES: JSON.stringify([
        { type: 'TRADING_CENTRAL', url: sourceUrl },
      ]),
      BROWSER_MODE: 'CDP',
      OLLAMA_API_KEY: 'test-key',
      IDEAS_LOG_PATH: join(dir, 'ideas.jsonl'),
      SEEN_STATE_PATH: join(dir, 'seen.json'),
      SCREENSHOT_DIR: join(dir, 'screenshots'),
      DEBUG_RUN_MAX_ENTRIES: '10',
    });
    const browser = new BrowserService(config);
    const dedup = new DedupService(config);
    dedup.onModuleInit();
    const jsonl = new JsonlLoggerService(config);

    const ocr = {
      recognize: jest.fn().mockResolvedValue({
        text: 'AUD/JPY 30 MIN 112.47 Target 113.06 Pivot 112.17',
        positionalText: '[x=0,y=0] AUD/JPY 30 MIN',
        confidence: 93,
      }),
    } as unknown as OcrService;
    const formatter = {
      format: jest.fn().mockImplementation((_ocr, ctx) => {
        const base = {
          provider: 'TRADING_CENTRAL' as const,
          instrument: 'AUD/JPY',
          timeframe: '30 MIN',
          direction: 'UP' as const,
          entry: 112.47,
          stopLoss: 112.17,
          pivot: 112.17,
          takeProfit: 113.06,
          target: 113.06,
          ideaTimestamp: '2026-07-14T05:02:34.000Z',
          capturedAt: ctx.capturedAt,
          sourceUrl,
          screenshotPath: ctx.screenshotPath,
        };
        return Promise.resolve({
          ideas: [{ ...base, hash: computeIdeaHash(base) }],
          rejected: [],
          model: 'ministral-3:3b',
          rawResponse: '{"signals":[]}',
          repaired: false,
        });
      }),
    } as unknown as OllamaFormatterService;
    const tradingCentral = new TradingCentralExtractor(ocr, formatter);
    const scraper = new ScraperService(
      config,
      browser,
      dedup,
      jsonl,
      tradingCentral,
      new AutochartistExtractor(),
    );
    const page = {
      isClosed: () => false,
    } as unknown as Page;
    scraper.setHooks({
      createPage: async () => page,
      navigate: async () => undefined,
      waitForContent: async () => undefined,
      isLoginWall: async () => false,
      takeScreenshot: async () => join(dir, 'tc.png'),
      closePage: async () => undefined,
    });

    const first = await scraper.runAllSources();
    const second = await scraper.runAllSources();

    expect(first.totalNew).toBe(1);
    expect(second.totalNew).toBe(0);
    expect(jsonl.readAll()).toHaveLength(1);
    expect(ocr.recognize).toHaveBeenCalledTimes(2);
    expect(formatter.format).toHaveBeenCalledTimes(2);

    const seen = JSON.parse(readFileSync(join(dir, 'seen.json'), 'utf8'));
    expect(seen.version).toBe(2);
    expect(Object.keys(seen.hashes)).toHaveLength(1);
    expect(Object.keys(seen.signals)).toHaveLength(1);
    expect(seen.runs).toHaveLength(2);
    expect(seen.runs.every((run: { status: string }) => run.status === 'success')).toBe(
      true,
    );
  });

  it('records browser failures without adding a seen hash', async () => {
    const sourceUrl =
      'https://secure.ic.com/TradingCentral/TradingCentral';
    const config = new AppConfigService();
    config.load({
      SOURCES: JSON.stringify([
        { type: 'TRADING_CENTRAL', url: sourceUrl },
      ]),
      BROWSER_MODE: 'CDP',
      OLLAMA_API_KEY: 'test-key',
      IDEAS_LOG_PATH: join(dir, 'ideas.jsonl'),
      SEEN_STATE_PATH: join(dir, 'seen.json'),
      SCREENSHOT_DIR: join(dir, 'screenshots'),
    });
    const browser = new BrowserService(config);
    const dedup = new DedupService(config);
    dedup.onModuleInit();
    const scraper = new ScraperService(
      config,
      browser,
      dedup,
      new JsonlLoggerService(config),
      new TradingCentralExtractor(),
      new AutochartistExtractor(),
    );
    scraper.setHooks({
      createPage: async () => {
        throw new BrowserAccessError(
          'matching_tab_not_found',
          'No matching tab',
        );
      },
    });

    const result = await scraper.runAllSources();
    expect(result.results[0].skippedReason).toBe('matching_tab_not_found');
    const seen = JSON.parse(readFileSync(join(dir, 'seen.json'), 'utf8'));
    expect(seen.hashes).toEqual({});
    expect(seen.runs).toHaveLength(1);
    expect(seen.runs[0]).toMatchObject({
      status: 'failed',
      stage: 'browser',
      error: 'matching_tab_not_found',
    });
  });
});
