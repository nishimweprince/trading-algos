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
import { Page } from 'playwright';

describe('SchedulerService schedules and overlap guard', () => {
  let dir: string;
  let scheduler: SchedulerService;
  let scraper: ScraperService;
  let browser: BrowserService;
  let runCount: number;
  let refreshCount: number;
  let reload: jest.SpyInstance;
  let releasePage: jest.SpyInstance;

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
      SIGNAL_CRON_EXPRESSION: '0 * * * *',
      AUTH_REFRESH_CRON_EXPRESSION: '*/5 * * * *',
      BROWSER_MODE: 'CDP',
    });
    browser = new BrowserService(config);
    const page = {
      isClosed: () => false,
      url: () =>
        'https://secure.icmarkets.com/TradingCentral/TradingCentral',
    } as unknown as Page;
    jest.spyOn(browser, 'getPage').mockResolvedValue(page);
    reload = jest.spyOn(browser, 'reload').mockResolvedValue(undefined);
    releasePage = jest
      .spyOn(browser, 'releasePage')
      .mockResolvedValue(undefined);
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
    refreshCount = 0;
    const originalRefresh = scraper.refreshAuthenticatedPages.bind(scraper);
    scraper.refreshAuthenticatedPages = async () => {
      refreshCount += 1;
      return originalRefresh();
    };

    const registry = new SchedulerRegistry();
    scheduler = new SchedulerService(config, scraper, registry);
    // Do not call onModuleInit (would start real cron); test handleCron directly
  });

  afterEach(() => {
    rmSync(dir, { recursive: true, force: true });
  });

  it('exposes independent cron expressions from config', () => {
    expect(scheduler.getSignalCronExpression()).toBe('0 * * * *');
    expect(scheduler.getAuthRefreshCronExpression()).toBe('*/5 * * * *');
  });

  it('runs scraper when not already running', async () => {
    await scheduler.handleCron();
    expect(runCount).toBe(1);
    expect(scheduler.running).toBe(false);
  });

  it('refreshes authentication without running signal extraction', async () => {
    await scheduler.handleAuthRefreshCron();

    expect(refreshCount).toBe(1);
    expect(runCount).toBe(0);
    expect(reload).toHaveBeenCalledTimes(1);
    expect(releasePage).toHaveBeenCalledTimes(1);
    expect(scheduler.running).toBe(false);
  });

  it('skips when previous run still in progress (overlap guard)', async () => {
    scheduler.setRunning(true);
    await scheduler.handleCron();
    expect(runCount).toBe(0);
    scheduler.setRunning(false);
  });

  it('does not refresh the page while signal extraction is running', async () => {
    scheduler.setRunning(true);
    await scheduler.handleAuthRefreshCron();

    expect(refreshCount).toBe(0);
    expect(reload).not.toHaveBeenCalled();
    scheduler.setRunning(false);
  });

  it('delays rather than loses a signal tick when refresh is already running', async () => {
    let finishRefresh!: () => void;
    const refreshPending = new Promise<void>((resolve) => {
      finishRefresh = resolve;
    });
    scraper.refreshAuthenticatedPages = async () => {
      refreshCount += 1;
      await refreshPending;
      return {
        startedAt: '2026-07-15T00:00:00.000Z',
        finishedAt: '2026-07-15T00:00:00.000Z',
        results: [],
        refreshed: 0,
      };
    };

    const refreshRun = scheduler.handleAuthRefreshCron();
    const signalRun = scheduler.handleSignalCron();
    expect(runCount).toBe(0);

    finishRefresh();
    await Promise.all([refreshRun, signalRun]);

    expect(refreshCount).toBe(1);
    expect(runCount).toBe(1);
  });
});
