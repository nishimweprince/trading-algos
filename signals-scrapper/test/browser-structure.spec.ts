import { readFileSync } from 'fs';
import { join } from 'path';

/**
 * Structural checks: Playwright usage, BROWSER_MODE branches, @Cron wiring,
 * login-wall skip, graceful shutdown — without requiring a live Chrome.
 */
describe('source structure (browser / schedule / shutdown)', () => {
  const root = join(__dirname, '..', 'src');

  it('BrowserService uses Playwright and both CDP and PERSISTENT modes', () => {
    const src = readFileSync(join(root, 'browser', 'browser.service.ts'), 'utf8');
    expect(src).toMatch(/from ['"]playwright['"]/);
    expect(src).toMatch(/connectOverCDP/);
    expect(src).toMatch(/launchPersistentContext/);
    expect(src).toMatch(/BROWSER_MODE|browserMode|CDP|PERSISTENT/);
    expect(src).toMatch(/screenshot/);
    // Split create/navigate so capture can attach before goto; reuse one page
    expect(src).toMatch(/async getPage\(/);
    expect(src).toMatch(/async navigate\(/);
    expect(src).toMatch(/releasePage/);
    expect(src).toMatch(/sharedPage/);
  });

  it('network capture is race-safe and extractors expose beginNetworkCapture', () => {
    const cap = readFileSync(join(root, 'scraper', 'network-capture.ts'), 'utf8');
    expect(cap).toMatch(/pending/);
    expect(cap).toMatch(/Promise\.all/);
    expect(cap).toMatch(/startNetworkCapture/);

    const iface = readFileSync(
      join(root, 'scraper', 'idea-extractor.interface.ts'),
      'utf8',
    );
    expect(iface).toMatch(/beginNetworkCapture/);
    expect(iface).toMatch(/networkUrlPatterns/);

    const scraper = readFileSync(join(root, 'scraper', 'scraper.service.ts'), 'utf8');
    const beginIdx = scraper.indexOf('beginNetworkCapture');
    const navIdx = scraper.indexOf('this.browser.navigate');
    expect(beginIdx).toBeGreaterThan(0);
    expect(navIdx).toBeGreaterThan(beginIdx);
    // Must release (keep open), not closePage, on the live scrape path
    expect(scraper).toMatch(/releasePage/);
    expect(scraper).toMatch(/getPage\(\)/);
  });

  it('SchedulerService registers cron and has overlap guard', () => {
    const src = readFileSync(
      join(root, 'scheduler', 'scheduler.service.ts'),
      'utf8',
    );
    expect(src).toMatch(/CronJob|@nestjs\/schedule/);
    expect(src).toMatch(/isRunning/);
    expect(src).toMatch(/runAllSources/);
    expect(src).toMatch(/CRON_EXPRESSION|cronExpression/);
  });

  it('ScraperService has login-wall skip path', () => {
    const src = readFileSync(join(root, 'scraper', 'scraper.service.ts'), 'utf8');
    expect(src).toMatch(/login_wall|isLoginWall|login wall/i);
    expect(src).toMatch(/timeout/i);
  });

  it('main enables shutdown hooks and fail-fast config', () => {
    const src = readFileSync(join(root, 'main.ts'), 'utf8');
    expect(src).toMatch(/enableShutdownHooks/);
    expect(src).toMatch(/loadAppConfig/);
    expect(src).toMatch(/process\.exit\(1\)/);
  });

  it('ShutdownService flushes seen state and closes browser', () => {
    const src = readFileSync(join(root, 'shutdown.service.ts'), 'utf8');
    expect(src).toMatch(/onModuleDestroy/);
    expect(src).toMatch(/flush/);
    expect(src).toMatch(/close/);
  });

  it('both extractors implement the same IdeaExtractor seam', () => {
    const iface = readFileSync(
      join(root, 'scraper', 'idea-extractor.interface.ts'),
      'utf8',
    );
    expect(iface).toMatch(/interface IdeaExtractor/);
    expect(iface).toMatch(/extract\(/);

    const tc = readFileSync(
      join(root, 'scraper', 'extractors', 'trading-central.extractor.ts'),
      'utf8',
    );
    const ac = readFileSync(
      join(root, 'scraper', 'extractors', 'autochartist.extractor.ts'),
      'utf8',
    );
    expect(tc).toMatch(/implements IdeaExtractor/);
    expect(ac).toMatch(/implements IdeaExtractor/);
    expect(tc).toMatch(/TRADING_CENTRAL/);
    expect(ac).toMatch(/AUTOCHARTIST/);
    expect(tc).toMatch(/beginNetworkCapture/);
    expect(ac).toMatch(/beginNetworkCapture/);
  });
});
