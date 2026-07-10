import { readFileSync } from 'fs';
import { join } from 'path';
import { AutochartistExtractor } from '../src/scraper/extractors/autochartist.extractor';
import { TradingCentralExtractor } from '../src/scraper/extractors/trading-central.extractor';
import { ExtractContext } from '../src/scraper/idea-extractor.interface';
import { computeIdeaHash } from '../src/dedup/hash';

const fixtures = join(__dirname, 'fixtures');

const tcCtx: ExtractContext = {
  sourceUrl: 'https://secure.icmarkets.com/TradingCentral/TradingCentral',
  provider: 'TRADING_CENTRAL',
  capturedAt: '2026-07-10T18:00:00.000Z',
  screenshotPath: '/tmp/tc.png',
};

const acCtx: ExtractContext = {
  sourceUrl: 'https://secure.icmarkets.com/AutoChartist/AutoChartist',
  provider: 'AUTOCHARTIST',
  capturedAt: '2026-07-10T18:00:00.000Z',
  screenshotPath: '/tmp/ac.png',
};

describe('TradingCentralExtractor', () => {
  const extractor = new TradingCentralExtractor();

  it('extracts ideas from API JSON fixture (network path)', () => {
    const payload = JSON.parse(
      readFileSync(join(fixtures, 'trading-central-api.json'), 'utf8'),
    );
    const ideas = extractor.extractFromApiJson(payload, tcCtx);
    expect(ideas.length).toBeGreaterThanOrEqual(3);

    const usdCad = ideas.find((i) => i.instrument === 'USD/CAD');
    expect(usdCad).toBeDefined();
    expect(usdCad!.provider).toBe('TRADING_CENTRAL');
    expect(usdCad!.direction).toBe('DOWN');
    expect(usdCad!.timeframe).toBe('30 MIN');
    expect(usdCad!.target).toBe(1.4115);
    expect(usdCad!.pivot).toBe(1.417);
    expect(usdCad!.expectedMovePips).toEqual([19, 39]);
    expect(usdCad!.levels?.support).toContain(1.4115);
    expect(usdCad!.sourceUrl).toBe(tcCtx.sourceUrl);
    expect(usdCad!.screenshotPath).toBe(tcCtx.screenshotPath);
    expect(usdCad!.raw).toBeDefined();
    // hash must be the real computeIdeaHash of the idea fields
    expect(usdCad!.hash).toBe(computeIdeaHash(usdCad!));

    const gbp = ideas.find((i) => i.instrument === 'GBP/USD');
    expect(gbp?.direction).toBe('DOWN'); // BEARISH -> DOWN
  });

  it('extracts ideas from DOM HTML fixture', () => {
    const html = readFileSync(join(fixtures, 'trading-central-dom.html'), 'utf8');
    const ideas = extractor.extractFromDomHtml(html, tcCtx);
    expect(ideas.length).toBe(2);
    expect(ideas.map((i) => i.instrument).sort()).toEqual([
      'AUD/USD',
      'USD/JPY',
    ]);
    const jpy = ideas.find((i) => i.instrument === 'USD/JPY')!;
    expect(jpy.direction).toBe('UP');
    expect(jpy.target).toBe(156.2);
    expect(jpy.hash).toBe(computeIdeaHash(jpy));
  });

  it('extract() uses networkPayloads when provided (no browser)', async () => {
    const payload = JSON.parse(
      readFileSync(join(fixtures, 'trading-central-api.json'), 'utf8'),
    );
    const ideas = await extractor.extract(null, {
      ...tcCtx,
      networkPayloads: [payload],
    });
    expect(ideas.length).toBeGreaterThanOrEqual(3);
  });
});

describe('AutochartistExtractor', () => {
  const extractor = new AutochartistExtractor();

  it('extracts ideas from API JSON fixture (network path)', () => {
    const payload = JSON.parse(
      readFileSync(join(fixtures, 'autochartist-api.json'), 'utf8'),
    );
    const ideas = extractor.extractFromApiJson(payload, acCtx);
    expect(ideas.length).toBeGreaterThanOrEqual(2);

    const gold = ideas.find((i) => i.instrument === 'XAU/USD');
    expect(gold).toBeDefined();
    expect(gold!.provider).toBe('AUTOCHARTIST');
    expect(gold!.direction).toBe('UP'); // BULLISH
    expect(gold!.target).toBe(2350.5);
    expect(gold!.timeframe).toBe('1 H');
    expect(gold!.hash).toBe(computeIdeaHash(gold!));

    const nas = ideas.find((i) => i.instrument === 'NAS100');
    expect(nas?.direction).toBe('DOWN');
  });

  it('extracts ideas from DOM HTML fixture', () => {
    const html = readFileSync(join(fixtures, 'autochartist-dom.html'), 'utf8');
    const ideas = extractor.extractFromDomHtml(html, acCtx);
    expect(ideas.length).toBe(2);
    expect(ideas.map((i) => i.instrument).sort()).toEqual([
      'EUR/GBP',
      'USD/CHF',
    ]);
    const chf = ideas.find((i) => i.instrument === 'USD/CHF')!;
    expect(chf.direction).toBe('DOWN'); // BEARISH
    expect(chf.hash).toBe(computeIdeaHash(chf));
  });

  it('extract() uses networkPayloads when provided (no browser)', async () => {
    const payload = JSON.parse(
      readFileSync(join(fixtures, 'autochartist-api.json'), 'utf8'),
    );
    const ideas = await extractor.extract(null, {
      ...acCtx,
      networkPayloads: [payload],
    });
    expect(ideas.length).toBeGreaterThanOrEqual(2);
  });
});
