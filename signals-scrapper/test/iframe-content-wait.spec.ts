import { readFileSync } from 'fs';
import { join } from 'path';
import {
  isTradingCentralContentReady,
  pickFrameUrl,
  waitForResearchContent,
  waitForTradingCentralCards,
  IFRAME_SELECTORS,
} from '../src/scraper/content-wait';
import { TradingCentralExtractor } from '../src/scraper/extractors/trading-central.extractor';
import { TRADING_CENTRAL_NETWORK_PATTERNS } from '../src/scraper/extractors/network-patterns';
import { loadAppConfig } from '../src/config/sources.schema';

const fixtures = join(__dirname, 'fixtures');

describe('iframe + content wait', () => {
  it('picks recognia iframe URL for TRADING_CENTRAL', () => {
    const chosen = pickFrameUrl(
      [
        'https://secure.icmarkets.com/TradingCentral/TradingCentral',
        'https://site.recognia.com/icmarketsau/serve.shtml?tkn=abc123',
        'about:blank',
      ],
      'TRADING_CENTRAL',
    );
    expect(chosen).toContain('recognia.com');
  });

  it('defines iframe#tradingcentral selector for Trading Central', () => {
    expect(IFRAME_SELECTORS.TRADING_CENTRAL.some((s) => s.includes('tradingcentral'))).toBe(
      true,
    );
    expect(
      IFRAME_SELECTORS.TRADING_CENTRAL.some((s) => s.includes('recognia')),
    ).toBe(true);
  });

  it('network patterns match recognia serve.shtml and API hosts', () => {
    const urls = [
      'https://site.recognia.com/icmarketsau/serve.shtml?tkn=xyz',
      'https://site.recognia.com/api/productanalysis/list',
      'https://cdn.recognia.com/story/json',
    ];
    for (const url of urls) {
      expect(
        TRADING_CENTRAL_NETWORK_PATTERNS.some((re) => re.test(url)),
      ).toBe(true);
    }
  });

  it('isTradingCentralContentReady detects loaded idea cards', () => {
    expect(
      isTradingCentralContentReady(
        'GBP/USD 30 MIN Expected Move 42 - 63 PIPS Target 1.3437 Pivot 1.3343 Trade',
      ),
    ).toBe(true);
    expect(
      isTradingCentralContentReady('TRADING CENTRAL\nForex\nCrypto'),
    ).toBe(false);
    expect(isTradingCentralContentReady('')).toBe(false);
  });

  it('waitForTradingCentralCards returns true when iframe text is ready', async () => {
    const frame = {
      url: () => 'https://site.recognia.com/icmarketsau/serve.shtml?tkn=abc',
      name: () => '',
      isDetached: () => false,
      evaluate: async () =>
        'AUD/USD 30 MIN Expected Move 21 - 32 PIPS Target 0.7030 Pivot 0.6981 Trade',
    };
    const fakePage = {
      frames: () => [frame],
      mainFrame: () => ({ url: () => 'https://secure.icmarkets.com/' }),
      waitForSelector: async () => null,
      waitForTimeout: async () => undefined,
      $: async () => null,
    };

    const ready = await waitForTradingCentralCards(fakePage as never, {
      timeoutMs: 1000,
      pollIntervalMs: 10,
      sleep: async () => undefined,
    });
    expect(ready).toBe(true);
  });

  it('waitForTradingCentralCards returns false when iframe never loads cards', async () => {
    const fakePage = {
      frames: () => [
        {
          url: () => 'https://site.recognia.com/icmarketsau/serve.shtml?tkn=abc',
          name: () => '',
          isDetached: () => false,
          evaluate: async () => 'TRADING CENTRAL',
        },
      ],
      mainFrame: () => ({ url: () => 'https://secure.icmarkets.com/' }),
      waitForSelector: async () => null,
      waitForTimeout: async () => undefined,
      $: async () => null,
    };

    const ready = await waitForTradingCentralCards(fakePage as never, {
      timeoutMs: 50,
      pollIntervalMs: 10,
      sleep: async () => undefined,
    });
    expect(ready).toBe(false);
  });

  it('CONTENT_WAIT_MS defaults to 10000', () => {
    const cfg = loadAppConfig({
      SOURCES: JSON.stringify([
        {
          type: 'TRADING_CENTRAL',
          url: 'https://secure.icmarkets.com/TradingCentral/TradingCentral',
        },
      ]),
    });
    expect(cfg.CONTENT_WAIT_MS).toBe(10000);
  });

  it('waitForResearchContent sleeps contentWaitMs after navigation (injectable sleep)', async () => {
    const sleeps: number[] = [];
    const fakePage = {
      frames: () => [
        {
          url: () => 'https://secure.icmarkets.com/TradingCentral/TradingCentral',
          name: () => '',
          isDetached: () => false,
        },
        {
          url: () =>
            'https://site.recognia.com/icmarketsau/serve.shtml?tkn=6a8YbC4s',
          name: () => '',
          isDetached: () => false,
        },
      ],
      mainFrame: () => ({ url: () => 'https://secure.icmarkets.com/' }),
      waitForSelector: async () => null,
      waitForTimeout: async () => undefined,
      $: async () => null,
    };

    const frame = await waitForResearchContent(fakePage as never, {
      provider: 'TRADING_CENTRAL',
      contentWaitMs: 10000,
      iframeTimeoutMs: 100,
      sleep: async (ms) => {
        sleeps.push(ms);
      },
    });

    expect(sleeps).toEqual([10000]);
    expect(frame?.url()).toContain('recognia.com');
  });

  it('TradingCentralExtractor parses host shell that only has the iframe (no false ideas)', () => {
    const extractor = new TradingCentralExtractor();
    const hostHtml = `
      <div id="content">
        <h1 class="h1">Trading Central</h1>
        <iframe id="tradingcentral"
          src="https://site.recognia.com/icmarketsau/serve.shtml?tkn=6a8YbC4sYWr9MWrA0Jtk7UO6qNZfR9"
          width="100%" height="900"></iframe>
      </div>
    `;
    const ideas = extractor.extractFromDomHtml(hostHtml, {
      sourceUrl: 'https://secure.icmarkets.com/TradingCentral/TradingCentral',
      provider: 'TRADING_CENTRAL',
      capturedAt: '2026-07-10T18:00:00.000Z',
    });
    // Shell has no card data — must not invent garbage ideas
    expect(ideas).toHaveLength(0);
  });

  it('TradingCentralExtractor parses idea cards from Recognia iframe HTML fixture', () => {
    const extractor = new TradingCentralExtractor();
    const html = readFileSync(join(fixtures, 'trading-central-dom.html'), 'utf8');
    const ideas = extractor.extractFromDomHtml(html, {
      sourceUrl: 'https://site.recognia.com/icmarketsau/serve.shtml?tkn=x',
      provider: 'TRADING_CENTRAL',
      capturedAt: '2026-07-10T18:00:00.000Z',
    });
    expect(ideas.length).toBe(2);
    expect(ideas.map((i) => i.instrument).sort()).toEqual(['AUD/USD', 'USD/JPY']);
  });

  it('TradingCentralExtractor parses free-text idea cards from iframe body text', () => {
    const extractor = new TradingCentralExtractor();
    const text = `
      USD/CAD 30 MIN
      Expected Move ↓ 19 - 39 PIPS
      Target 1.4115
      Pivot 1.4170
      Trade
      EUR/USD 1 H
      Expected Move ↑ 17 - 32 PIPS
      Target 1.0850
      Pivot 1.0800
      Trade
    `;
    const ideas = extractor.parseIdeasFromPlainText(text, {
      sourceUrl: 'https://site.recognia.com/x',
      provider: 'TRADING_CENTRAL',
      capturedAt: '2026-07-10T18:00:00.000Z',
    });
    expect(ideas.length).toBeGreaterThanOrEqual(2);
    expect(ideas.some((i) => i.instrument.includes('USD') && i.direction === 'DOWN')).toBe(
      true,
    );
    expect(ideas.some((i) => i.instrument.includes('EUR'))).toBe(true);
  });

  it('scraper source waits for content after navigate before login/screenshot', () => {
    const src = readFileSync(
      join(__dirname, '..', 'src', 'scraper', 'scraper.service.ts'),
      'utf8',
    );
    expect(src).toMatch(/waitForResearchContent/);
    expect(src).toMatch(/contentWaitMs/);
    // Use call site, not the import line
    const navIdx = src.indexOf('await this.browser.navigate');
    const waitIdx = src.indexOf('await waitForResearchContent');
    const loginBlock = src.indexOf('// ---- 3. Login wall');
    expect(navIdx).toBeGreaterThan(0);
    expect(waitIdx).toBeGreaterThan(navIdx);
    expect(loginBlock).toBeGreaterThan(waitIdx);
  });
});
