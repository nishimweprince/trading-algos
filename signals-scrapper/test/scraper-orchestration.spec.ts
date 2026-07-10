import { mkdtempSync, mkdirSync, readFileSync, rmSync, writeFileSync } from 'fs';
import { tmpdir } from 'os';
import { join } from 'path';
import { BrowserService } from '../src/browser/browser.service';
import { AppConfigService } from '../src/config/app-config.service';
import { DedupService } from '../src/dedup/dedup.service';
import { JsonlLoggerService } from '../src/logging/jsonl-logger.service';
import { AutochartistExtractor } from '../src/scraper/extractors/autochartist.extractor';
import { TradingCentralExtractor } from '../src/scraper/extractors/trading-central.extractor';
import { isLoginWallFromSnapshot } from '../src/scraper/login-wall';
import { ScraperService } from '../src/scraper/scraper.service';

const fixtures = join(__dirname, 'fixtures');

function buildServices(dir: string) {
  const config = new AppConfigService();
  config.load({
    SOURCES: JSON.stringify([
      {
        type: 'TRADING_CENTRAL',
        url: 'https://secure.icmarkets.com/TradingCentral/TradingCentral',
      },
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
    NAV_TIMEOUT_MS: '5000',
    CONTENT_WAIT_MS: '0',
  });

  const browser = new BrowserService(config);
  const dedup = new DedupService(config);
  dedup.onModuleInit();
  const jsonl = new JsonlLoggerService(config);
  const tc = new TradingCentralExtractor();
  const ac = new AutochartistExtractor();
  const scraper = new ScraperService(config, browser, dedup, jsonl, tc, ac);
  return { config, browser, dedup, jsonl, scraper };
}

describe('ScraperService multi-source orchestration', () => {
  let dir: string;

  beforeEach(() => {
    dir = mkdtempSync(join(tmpdir(), 'scrape-orch-'));
    mkdirSync(join(dir, 'screenshots'), { recursive: true });
  });

  afterEach(() => {
    rmSync(dir, { recursive: true, force: true });
  });

  it('runs both TRADING_CENTRAL and AUTOCHARTIST via fixture network payloads', async () => {
    const { scraper, jsonl } = buildServices(dir);
    const tcPayload = JSON.parse(
      readFileSync(join(fixtures, 'trading-central-api.json'), 'utf8'),
    );
    const acPayload = JSON.parse(
      readFileSync(join(fixtures, 'autochartist-api.json'), 'utf8'),
    );

    const shotPaths: string[] = [];

    scraper.setHooks({
      openPage: async () => null,
      waitForContent: async () => undefined,
      isLoginWall: async () => false,
      takeScreenshot: async (_page, sourceType) => {
        const p = join(dir, 'screenshots', `${sourceType}_test.png`);
        // ensure path is recorded (write a tiny placeholder so file exists)
        writeFileSync(p, 'fake-png');
        shotPaths.push(p);
        return p;
      },
      networkPayloads: (source) => {
        if (source.type === 'TRADING_CENTRAL') return [tcPayload];
        if (source.type === 'AUTOCHARTIST') return [acPayload];
        return undefined;
      },
    });

    const result = await scraper.runAllSources();
    expect(result.results).toHaveLength(2);
    expect(result.results.every((r) => r.ok)).toBe(true);
    expect(result.totalNew).toBeGreaterThanOrEqual(5);

    for (const r of result.results) {
      expect(r.screenshotPath).toBeDefined();
      expect(r.extracted).toBeGreaterThan(0);
      expect(r.newIdeas).toBe(r.extracted);
    }

    const logged = jsonl.readAll();
    expect(logged.length).toBe(result.totalNew);
    expect(logged.some((i) => i.provider === 'TRADING_CENTRAL')).toBe(true);
    expect(logged.some((i) => i.provider === 'AUTOCHARTIST')).toBe(true);
    expect(logged.every((i) => !!i.hash && !!i.instrument)).toBe(true);
    expect(logged.every((i) => !!i.screenshotPath)).toBe(true);

    // Second run with same payloads — zero new lines
    const result2 = await scraper.runAllSources();
    expect(result2.totalNew).toBe(0);
    expect(jsonl.readAll()).toHaveLength(logged.length);

    // Changed target in TC payload produces a new line
    const mutated = structuredClone(tcPayload) as typeof tcPayload;
    mutated.ideas[0].target = 1.4;
    scraper.setHooks({
      openPage: async () => null,
      waitForContent: async () => undefined,
      isLoginWall: async () => false,
      takeScreenshot: async (_p, t) => join(dir, 'screenshots', `${t}_2.png`),
      networkPayloads: (source) => {
        if (source.type === 'TRADING_CENTRAL') return [mutated];
        if (source.type === 'AUTOCHARTIST') return [acPayload];
        return undefined;
      },
    });
    const result3 = await scraper.runAllSources();
    expect(result3.totalNew).toBe(1);
    const after = jsonl.readAll();
    expect(after.length).toBe(logged.length + 1);
    expect(after[after.length - 1].target).toBe(1.4);
  });

  it('skips source on login wall without writing garbage ideas', async () => {
    const { scraper, jsonl } = buildServices(dir);
    // Login wall is checked after content wait; skip wait in tests.
    scraper.setHooks({
      openPage: async () => null,
      waitForContent: async () => undefined,
      isLoginWall: async () => true,
      takeScreenshot: async () => {
        const p = join(dir, 'screenshots', 'login.png');
        writeFileSync(p, 'x');
        return p;
      },
    });

    const result = await scraper.runAllSources();
    expect(result.totalNew).toBe(0);
    expect(result.results.every((r) => r.skippedReason === 'login_wall')).toBe(
      true,
    );
    expect(result.results.every((r) => r.screenshotPath)).toBe(true);
    expect(jsonl.readAll()).toHaveLength(0);
  });

  it('records timeout skip without writing ideas', async () => {
    const { scraper, jsonl } = buildServices(dir);
    scraper.setHooks({
      openPage: async () => {
        const err = new Error('Navigation timeout of 5000 ms exceeded');
        err.name = 'TimeoutError';
        throw err;
      },
    });
    const result = await scraper.runAllSources();
    expect(result.totalNew).toBe(0);
    expect(result.results.every((r) => r.skippedReason === 'timeout')).toBe(
      true,
    );
    expect(jsonl.readAll()).toHaveLength(0);
  });
});

describe('login-wall pure helper', () => {
  it('detects login page HTML', () => {
    const html = readFileSync(join(fixtures, 'login-wall.html'), 'utf8');
    expect(
      isLoginWallFromSnapshot({
        url: 'https://secure.icmarkets.com/login',
        html,
      }),
    ).toBe(true);
  });

  it('does not flag authenticated research HTML', () => {
    const html = readFileSync(join(fixtures, 'trading-central-dom.html'), 'utf8');
    expect(
      isLoginWallFromSnapshot({
        url: 'https://secure.icmarkets.com/TradingCentral/TradingCentral',
        html: html + '<a href="/logout">Logout</a>',
      }),
    ).toBe(false);
  });
});
