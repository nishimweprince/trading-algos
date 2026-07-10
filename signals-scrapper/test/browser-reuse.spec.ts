import { readFileSync } from 'fs';
import { join } from 'path';
import { BrowserService } from '../src/browser/browser.service';
import { AppConfigService } from '../src/config/app-config.service';
import { Page } from 'playwright';

/**
 * Unit tests for page/window reuse without launching real Chromium.
 * We stub ensureContext + context.pages / newPage to prove getPage returns
 * the same instance and releasePage does not close it.
 */
describe('BrowserService page reuse', () => {
  function buildService() {
    const config = new AppConfigService();
    config.load({
      SOURCES: JSON.stringify([
        {
          type: 'TRADING_CENTRAL',
          url: 'https://secure.icmarkets.com/TradingCentral/TradingCentral',
        },
      ]),
      BROWSER_MODE: 'PERSISTENT',
      USER_DATA_DIR: './.chrome-profile',
      NAV_TIMEOUT_MS: '5000',
    });
    return new BrowserService(config);
  }

  function fakePage(id: string): Page & { __id: string; __closed: boolean } {
    const page = {
      __id: id,
      __closed: false,
      isClosed: () => page.__closed,
      setDefaultTimeout: () => undefined,
      setDefaultNavigationTimeout: () => undefined,
      close: async () => {
        page.__closed = true;
      },
      goto: async () => null,
    };
    return page as unknown as Page & { __id: string; __closed: boolean };
  }

  it('getPage reuses the same page across multiple calls (no second newPage)', async () => {
    const service = buildService();
    const page1 = fakePage('p1');
    let newPageCalls = 0;

    const fakeContext = {
      pages: () => (newPageCalls === 0 ? [] : [page1]),
      newPage: async () => {
        newPageCalls += 1;
        return page1;
      },
    };

    jest
      .spyOn(service, 'ensureContext')
      .mockResolvedValue(fakeContext as never);

    const a = await service.getPage();
    const b = await service.getPage();
    const c = await service.getPage();

    expect(a).toBe(page1);
    expect(b).toBe(page1);
    expect(c).toBe(page1);
    expect(newPageCalls).toBe(1);
    expect(service.hasSharedPage()).toBe(true);
  });

  it('getPage reuses an already-open context page without calling newPage', async () => {
    const service = buildService();
    const existing = fakePage('existing');
    let newPageCalls = 0;

    const fakeContext = {
      pages: () => [existing],
      newPage: async () => {
        newPageCalls += 1;
        return fakePage('should-not');
      },
    };

    jest
      .spyOn(service, 'ensureContext')
      .mockResolvedValue(fakeContext as never);

    const a = await service.getPage();
    const b = await service.getPage();
    expect(a).toBe(existing);
    expect(b).toBe(existing);
    expect(newPageCalls).toBe(0);
  });

  it('releasePage does not close the page (window stays for next cron tick)', async () => {
    const service = buildService();
    const page = fakePage('keep');
    const fakeContext = {
      pages: () => [page],
      newPage: async () => page,
    };
    jest
      .spyOn(service, 'ensureContext')
      .mockResolvedValue(fakeContext as never);

    await service.getPage();
    await service.releasePage(page);

    expect(page.__closed).toBe(false);
    expect(service.hasSharedPage()).toBe(true);

    // Next getPage still returns the same open page
    const again = await service.getPage();
    expect(again).toBe(page);
  });

  it('closePage hard-closes and clears shared reference (shutdown/teardown)', async () => {
    const service = buildService();
    const page = fakePage('close-me');
    jest.spyOn(service, 'ensureContext').mockResolvedValue({
      pages: () => [page],
      newPage: async () => page,
    } as never);

    await service.getPage();
    await service.closePage(page);
    expect(page.__closed).toBe(true);
    expect(service.hasSharedPage()).toBe(false);
  });

  it('scraper live path source uses getPage + releasePage (not closePage)', () => {
    const src = readFileSync(
      join(__dirname, '..', 'src', 'scraper', 'scraper.service.ts'),
      'utf8',
    );
    expect(src).toMatch(/this\.browser\.getPage\(\)/);
    expect(src).toMatch(/this\.browser\.releasePage\(page\)/);
    // Live finally block must not hard-close
    expect(src).not.toMatch(/this\.browser\.closePage\(page\)/);
  });
});
