import {
  createMockPageForCapture,
  startNetworkCapture,
} from '../src/scraper/network-capture';
import { TradingCentralExtractor } from '../src/scraper/extractors/trading-central.extractor';
import { AutochartistExtractor } from '../src/scraper/extractors/autochartist.extractor';
import { BrowserService } from '../src/browser/browser.service';
import { AppConfigService } from '../src/config/app-config.service';
import { DedupService } from '../src/dedup/dedup.service';
import { JsonlLoggerService } from '../src/logging/jsonl-logger.service';
import { ScraperService } from '../src/scraper/scraper.service';
import { mkdtempSync, mkdirSync, readFileSync, rmSync, writeFileSync } from 'fs';
import { tmpdir } from 'os';
import { join } from 'path';
import { Page } from 'playwright';

const fixtures = join(__dirname, 'fixtures');

function slowJsonResponse(opts: {
  url: string;
  body: unknown;
  delayMs: number;
  contentType?: string;
}) {
  return {
    url: () => opts.url,
    headers: () => ({
      'content-type': opts.contentType ?? 'application/json',
    }),
    json: () =>
      new Promise<unknown>((resolve) => {
        setTimeout(() => resolve(opts.body), opts.delayMs);
      }),
  };
}

describe('startNetworkCapture (race-safe + pre-nav)', () => {
  it('awaits in-flight response.json() before finish returns (no race drop)', async () => {
    const { page, emitResponse } = createMockPageForCapture();
    const session = startNetworkCapture(page, {
      urlPatterns: [/trading.?central/i, /ideas/i],
    });

    // Simulate research API response whose json() takes 80ms
    emitResponse(
      slowJsonResponse({
        url: 'https://api.example.com/trading-central/ideas',
        body: {
          ideas: [
            {
              instrument: 'USD/CAD',
              timeframe: '30 MIN',
              direction: 'DOWN',
              target: 1.4115,
              ideaTimestamp: '2026-07-10T17:10:00.000Z',
            },
          ],
        },
        delayMs: 80,
      }),
    );

    // finish() must wait for the pending json() — not return empty
    const payloads = await session.finish({ settleMs: 0 });
    expect(payloads).toHaveLength(1);
    expect((payloads[0] as { ideas: unknown[] }).ideas[0]).toMatchObject({
      instrument: 'USD/CAD',
    });
  });

  it('captures multiple slow concurrent responses without dropping', async () => {
    const { page, emitResponse } = createMockPageForCapture();
    const session = startNetworkCapture(page, {
      urlPatterns: [/autochartist/i, /pattern/i],
    });

    emitResponse(
      slowJsonResponse({
        url: 'https://cdn.example.com/autochartist/patterns?a=1',
        body: { patterns: [{ symbol: 'XAU/USD', timeframe: '1 H', trend: 'UP' }] },
        delayMs: 60,
      }),
    );
    emitResponse(
      slowJsonResponse({
        url: 'https://cdn.example.com/autochartist/patterns?a=2',
        body: { patterns: [{ symbol: 'EUR/USD', timeframe: '15 MIN', trend: 'DOWN' }] },
        delayMs: 120,
      }),
    );

    // Ignore non-matching URLs
    emitResponse(
      slowJsonResponse({
        url: 'https://cdn.example.com/static/logo.png',
        body: { noop: true },
        delayMs: 10,
        contentType: 'image/png',
      }),
    );

    const payloads = await session.finish({ settleMs: 0 });
    expect(payloads).toHaveLength(2);
  });

  it('records navigation-time responses when listener is attached before emit (pre-nav model)', async () => {
    const { page, emitResponse } = createMockPageForCapture();
    const order: string[] = [];

    // 1. Attach capture FIRST
    const session = startNetworkCapture(page, {
      urlPatterns: [/trading-central/i],
    });
    order.push('capture-started');

    // 2. Simulate navigation-time API response (would be missed if attach were after goto)
    order.push('nav-response');
    emitResponse(
      slowJsonResponse({
        url: 'https://secure.icmarkets.com/api/trading-central/ideas',
        body: { ideas: [{ instrument: 'EUR/USD', timeframe: '1 H', direction: 'UP' }] },
        delayMs: 30,
      }),
    );

    order.push('nav-done');
    const payloads = await session.finish({ settleMs: 0 });
    order.push('finish-done');

    expect(order).toEqual([
      'capture-started',
      'nav-response',
      'nav-done',
      'finish-done',
    ]);
    expect(payloads).toHaveLength(1);
  });

  it('does NOT capture responses that fire before the listener is attached (documents the bug we fixed)', async () => {
    const { page, emitResponse } = createMockPageForCapture();

    // Response during "goto" before any listener — this is the old broken path
    emitResponse(
      slowJsonResponse({
        url: 'https://api.example.com/trading-central/ideas',
        body: { ideas: [{ instrument: 'MISS/ME', timeframe: '1 H', direction: 'UP' }] },
        delayMs: 5,
      }),
    );

    const session = startNetworkCapture(page, {
      urlPatterns: [/trading-central/i],
    });
    const payloads = await session.finish({ settleMs: 0 });
    expect(payloads).toHaveLength(0);
  });
});

describe('extractor beginNetworkCapture uses shared race-safe session', () => {
  it('TradingCentralExtractor.beginNetworkCapture captures matching URL', async () => {
    const { page, emitResponse } = createMockPageForCapture();
    const extractor = new TradingCentralExtractor();
    const session = extractor.beginNetworkCapture(page);
    emitResponse(
      slowJsonResponse({
        url: 'https://x/trading-central/opinion',
        body: JSON.parse(
          readFileSync(join(fixtures, 'trading-central-api.json'), 'utf8'),
        ),
        delayMs: 40,
      }),
    );
    const payloads = await session.finish({ settleMs: 0 });
    expect(payloads.length).toBeGreaterThanOrEqual(1);
    const ideas = extractor.extractFromApiJson(payloads[0], {
      sourceUrl: 'https://example.com',
      provider: 'TRADING_CENTRAL',
      capturedAt: '2026-07-10T18:00:00.000Z',
    });
    expect(ideas.length).toBeGreaterThanOrEqual(3);
  });

  it('AutochartistExtractor.beginNetworkCapture captures matching URL', async () => {
    const { page, emitResponse } = createMockPageForCapture();
    const extractor = new AutochartistExtractor();
    const session = extractor.beginNetworkCapture(page);
    emitResponse(
      slowJsonResponse({
        url: 'https://x/autochartist/patterns',
        body: JSON.parse(
          readFileSync(join(fixtures, 'autochartist-api.json'), 'utf8'),
        ),
        delayMs: 40,
      }),
    );
    const payloads = await session.finish({ settleMs: 0 });
    const ideas = extractor.extractFromApiJson(payloads[0], {
      sourceUrl: 'https://example.com',
      provider: 'AUTOCHARTIST',
      capturedAt: '2026-07-10T18:00:00.000Z',
    });
    expect(ideas.length).toBeGreaterThanOrEqual(2);
  });
});

describe('ScraperService live path: capture before navigate', () => {
  let dir: string;

  beforeEach(() => {
    dir = mkdtempSync(join(tmpdir(), 'netcap-orch-'));
    mkdirSync(join(dir, 'screenshots'), { recursive: true });
  });

  afterEach(() => {
    rmSync(dir, { recursive: true, force: true });
  });

  function buildScraper() {
    const config = new AppConfigService();
    config.load({
      SOURCES: JSON.stringify([
        {
          type: 'AUTOCHARTIST',
          url: 'https://secure.icmarkets.com/AutoChartist/AutoChartist',
        },
      ]),
      IDEAS_LOG_PATH: join(dir, 'ideas.jsonl'),
      SEEN_STATE_PATH: join(dir, 'seen.json'),
      SCREENSHOT_DIR: join(dir, 'screenshots'),
      BROWSER_MODE: 'CDP',
      CRON_EXPRESSION: '*/15 * * * *',
      CONTENT_WAIT_MS: '0',
    });
    const browser = new BrowserService(config);
    const dedup = new DedupService(config);
    dedup.onModuleInit();
    const jsonl = new JsonlLoggerService(config);
    return new ScraperService(
      config,
      browser,
      dedup,
      jsonl,
      new TradingCentralExtractor(),
      new AutochartistExtractor(),
    );
  }

  it('starts network capture before navigate and collects navigation-time responses', async () => {
    const scraper = buildScraper();
    const { page, emitResponse } = createMockPageForCapture();
    const order: string[] = [];
    const autochartistPayload = JSON.parse(
      readFileSync(join(fixtures, 'autochartist-api.json'), 'utf8'),
    );

    scraper.setHooks({
      createPage: async () => {
        order.push('createPage');
        return page;
      },
      onCaptureStarted: () => {
        order.push('captureStarted');
      },
      navigate: async () => {
        order.push('navigate');
        // Fire research API response during navigation (the critical case)
        emitResponse(
          slowJsonResponse({
            url: 'https://api.autochartist.com/patterns',
            body: autochartistPayload,
            delayMs: 50,
          }),
        );
        order.push('navResponseEmitted');
      },
      waitForContent: async () => {
        order.push('waitForContent');
      },
      isLoginWall: async () => false,
      takeScreenshot: async () => {
        order.push('screenshot');
        const p = join(dir, 'screenshots', 'tc.png');
        writeFileSync(p, 'x');
        return p;
      },
      closePage: async () => {
        order.push('closePage');
      },
    });

    const result = await scraper.runAllSources();

    // Ordering contract: capture before navigate; content wait after navigate
    const captureIdx = order.indexOf('captureStarted');
    const navIdx = order.indexOf('navigate');
    const waitIdx = order.indexOf('waitForContent');
    expect(captureIdx).toBeGreaterThanOrEqual(0);
    expect(navIdx).toBeGreaterThan(captureIdx);
    expect(order.indexOf('createPage')).toBeLessThan(captureIdx);
    expect(waitIdx).toBeGreaterThan(navIdx);
    expect(order.indexOf('screenshot')).toBeGreaterThan(waitIdx);

    expect(result.totalNew).toBeGreaterThanOrEqual(2);
    expect(result.results[0].ok).toBe(true);
    expect(result.results[0].screenshotPath).toBeDefined();

    const logged = readFileSync(join(dir, 'ideas.jsonl'), 'utf8')
      .trim()
      .split('\n')
      .map((l) => JSON.parse(l));
    expect(logged.length).toBe(result.totalNew);
    expect(logged[0].provider).toBe('AUTOCHARTIST');
    expect(logged[0].instrument).toBeTruthy();
  });

  it('structural: BrowserService exposes getPage + navigate; openPage reuses getPage', () => {
    const src = readFileSync(
      join(__dirname, '..', 'src', 'browser', 'browser.service.ts'),
      'utf8',
    );
    expect(src).toMatch(/async getPage\(/);
    expect(src).toMatch(/async navigate\(/);
    expect(src).toMatch(/async openPage\(/);
    expect(src).toMatch(/async releasePage\(/);
    // openPage composes getPage + navigate (shared window)
    expect(src).toMatch(/await this\.getPage\(\)/);
    expect(src).toMatch(/await this\.navigate\(/);
  });

  it('structural: ScraperService attaches capture before navigate on live path', () => {
    const src = readFileSync(
      join(__dirname, '..', 'src', 'scraper', 'scraper.service.ts'),
      'utf8',
    );
    // Order in source: beginNetworkCapture then navigate
    const beginIdx = src.indexOf('beginNetworkCapture');
    const navigateIdx = src.indexOf('this.browser.navigate');
    expect(beginIdx).toBeGreaterThan(0);
    expect(navigateIdx).toBeGreaterThan(beginIdx);
    expect(src).toMatch(/finish\(\{\s*settleMs/);
    expect(src).toMatch(/getPage\(\)/);
    expect(src).toMatch(/releasePage/);
  });
});

// silence unused Page import lint if any
void (null as unknown as Page);
