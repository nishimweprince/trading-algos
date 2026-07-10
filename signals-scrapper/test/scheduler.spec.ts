import { mkdtempSync, rmSync } from 'fs';
import { tmpdir } from 'os';
import { join } from 'path';
import { BrowserService } from '../src/browser/browser.service';
import { AppConfigService } from '../src/config/app-config.service';
import { DedupService } from '../src/dedup/dedup.service';
import { JsonlLoggerService } from '../src/logging/jsonl-logger.service';
import { AutochartistExtractor } from '../src/scraper/extractors/autochartist.extractor';
import { TradingCentralExtractor } from '../src/scraper/extractors/trading-central.extractor';
import { ScraperService } from '../src/scraper/scraper.service';
import { SchedulerService } from '../src/scheduler/scheduler.service';
import { SchedulerRegistry } from '@nestjs/schedule';

describe('SchedulerService overlap guard', () => {
  let dir: string;
  let scheduler: SchedulerService;
  let scraper: ScraperService;
  let runCount: number;

  beforeEach(() => {
    dir = mkdtempSync(join(tmpdir(), 'sched-'));
    const config = new AppConfigService();
    config.load({
      SOURCES: JSON.stringify([
        {
          type: 'TRADING_CENTRAL',
          url: 'https://secure.icmarkets.com/TradingCentral/TradingCentral',
        },
      ]),
      IDEAS_LOG_PATH: join(dir, 'ideas.jsonl'),
      SEEN_STATE_PATH: join(dir, 'seen.json'),
      SCREENSHOT_DIR: join(dir, 'screenshots'),
      CRON_EXPRESSION: '*/15 * * * *',
      BROWSER_MODE: 'CDP',
    });
    const browser = new BrowserService(config);
    const dedup = new DedupService(config);
    dedup.onModuleInit();
    const jsonl = new JsonlLoggerService(config);
    scraper = new ScraperService(
      config,
      browser,
      dedup,
      jsonl,
      new TradingCentralExtractor(),
      new AutochartistExtractor(),
    );
    runCount = 0;
    scraper.setHooks({
      openPage: async () => null,
      waitForContent: async () => undefined,
      isLoginWall: async () => false,
      takeScreenshot: async () => undefined,
      networkPayloads: () => [
        {
          ideas: [
            {
              instrument: 'USD/CAD',
              timeframe: '30 MIN',
              direction: 'DOWN',
              target: 1.4,
              ideaTimestamp: '2026-07-10T17:10:00.000Z',
            },
          ],
        },
      ],
    });

    // Spy on runAllSources
    const original = scraper.runAllSources.bind(scraper);
    scraper.runAllSources = async () => {
      runCount += 1;
      return original();
    };

    const registry = new SchedulerRegistry();
    scheduler = new SchedulerService(config, scraper, registry);
    // Do not call onModuleInit (would start real cron); test handleCron directly
  });

  afterEach(() => {
    rmSync(dir, { recursive: true, force: true });
  });

  it('exposes cron expression from config', () => {
    expect(scheduler.getCronExpression()).toBe('*/15 * * * *');
  });

  it('runs scraper when not already running', async () => {
    await scheduler.handleCron();
    expect(runCount).toBe(1);
    expect(scheduler.running).toBe(false);
  });

  it('skips when previous run still in progress (overlap guard)', async () => {
    scheduler.setRunning(true);
    await scheduler.handleCron();
    expect(runCount).toBe(0);
    scheduler.setRunning(false);
  });
});
