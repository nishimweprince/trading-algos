import {
  BrowserAccessError,
  BrowserService,
  normalizeTabUrl,
  tabMatchesSource,
} from '../src/browser/browser.service';
import { AppConfigService } from '../src/config/app-config.service';
import { Page } from 'playwright';

describe('BrowserService existing CDP tab selection', () => {
  function service(): BrowserService {
    const config = new AppConfigService();
    config.load({
      SOURCES: JSON.stringify([
        {
          type: 'TRADING_CENTRAL',
          url: 'https://secure.ic.com/TradingCentral/TradingCentral',
        },
      ]),
      BROWSER_MODE: 'CDP',
    });
    return new BrowserService(config);
  }

  function page(url: string): Page {
    return {
      url: () => url,
      isClosed: () => false,
      setDefaultTimeout: () => undefined,
      setDefaultNavigationTimeout: () => undefined,
    } as unknown as Page;
  }

  it('normalizes query strings, fragments, casing, and trailing slash', () => {
    expect(
      normalizeTabUrl(
        'https://SECURE.IC.com/TradingCentral/TradingCentral/?session=1#x',
      ),
    ).toBe('https://secure.ic.com/tradingcentral/tradingcentral');
    expect(
      tabMatchesSource(
        'https://secure.ic.com/TradingCentral/TradingCentral/?session=1',
        'https://secure.ic.com/TradingCentral/TradingCentral',
      ),
    ).toBe(true);
  });

  it('selects the matching tab without calling newPage', async () => {
    const browser = service();
    const unrelated = page('https://example.com');
    const matching = page(
      'https://secure.ic.com/TradingCentral/TradingCentral?token=abc',
    );
    const newPage = jest.fn();
    jest.spyOn(browser, 'ensureContext').mockResolvedValue({
      pages: () => [unrelated, matching],
      newPage,
    } as never);

    await expect(
      browser.getPage(
        'https://secure.ic.com/TradingCentral/TradingCentral',
      ),
    ).resolves.toBe(matching);
    expect(newPage).not.toHaveBeenCalled();
  });

  it('returns a typed failure when no matching tab exists', async () => {
    const browser = service();
    jest.spyOn(browser, 'ensureContext').mockResolvedValue({
      pages: () => [page('https://example.com')],
      newPage: jest.fn(),
    } as never);

    await expect(
      browser.getPage(
        'https://secure.ic.com/TradingCentral/TradingCentral',
      ),
    ).rejects.toMatchObject<Partial<BrowserAccessError>>({
      code: 'matching_tab_not_found',
    });
  });

  it('reloads the selected tab and does not close external Chrome on teardown', async () => {
    const browser = service();
    const reload = jest.fn().mockResolvedValue(undefined);
    const selected = {
      url: () => 'https://secure.ic.com/TradingCentral/TradingCentral',
      reload,
      isClosed: () => false,
      setDefaultTimeout: () => undefined,
      setDefaultNavigationTimeout: () => undefined,
    } as unknown as Page;
    await browser.reload(selected);
    expect(reload).toHaveBeenCalledWith(
      expect.objectContaining({ waitUntil: 'domcontentloaded' }),
    );

    const close = jest.fn();
    Object.assign(browser as unknown as Record<string, unknown>, {
      browser: { close },
      context: { pages: () => [selected] },
      ownsBrowser: false,
    });
    await browser.close();
    expect(close).toHaveBeenCalledTimes(1);
  });
});
