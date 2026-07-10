import { readFileSync } from 'fs';
import { join } from 'path';
import {
  parseTradingCentralCardChunk,
  parseTradingCentralText,
  splitTradingCentralCards,
} from '../src/scraper/extractors/tc-card-parser';
import { TradingCentralExtractor } from '../src/scraper/extractors/trading-central.extractor';
import { ExtractContext } from '../src/scraper/idea-extractor.interface';

const fixtures = join(__dirname, 'fixtures');

const ctx: ExtractContext = {
  sourceUrl: 'https://secure.icmarkets.com/TradingCentral/TradingCentral',
  provider: 'TRADING_CENTRAL',
  capturedAt: '2026-07-10T08:00:00.000Z',
  screenshotPath: '/tmp/tc.png',
};

describe('parseTradingCentralText (live Recognia layout)', () => {
  const liveText = readFileSync(
    join(fixtures, 'trading-central-live-text.txt'),
    'utf8',
  );

  it('splits into one chunk per Trade button', () => {
    const chunks = splitTradingCentralCards(liveText);
    expect(chunks.length).toBeGreaterThanOrEqual(6);
  });

  it('extracts all six cards from the screenshot-shaped fixture text', () => {
    const ideas = parseTradingCentralText(liveText, ctx);
    expect(ideas.length).toBe(6);

    const byInst = Object.fromEntries(ideas.map((i) => [i.instrument, i]));
    expect(byInst['USD/CAD']).toBeDefined();
    expect(byInst['USD/CAD'].direction).toBe('DOWN');
    expect(byInst['USD/CAD'].timeframe).toBe('30 MIN');
    expect(byInst['USD/CAD'].target).toBe(1.4115);
    expect(byInst['USD/CAD'].pivot).toBe(1.417);
    expect(byInst['USD/CAD'].expectedMovePips).toEqual([19, 39]);
    expect(byInst['USD/CAD'].provider).toBe('TRADING_CENTRAL');
    expect(byInst['USD/CAD'].hash).toMatch(/^[a-f0-9]{64}$/);

    expect(byInst['AUD/USD'].direction).toBe('UP');
    expect(byInst['AUD/USD'].target).toBe(0.6985);
    expect(byInst['AUD/USD'].expectedMovePips).toEqual([17, 32]);

    expect(byInst['USD/CHF'].direction).toBe('DOWN');
    expect(byInst['USD/CHF'].target).toBe(0.8);

    expect(byInst['EUR/USD'].direction).toBe('UP');
    expect(Object.keys(byInst).sort()).toEqual([
      'AUD/USD',
      'EUR/USD',
      'GBP/USD',
      'USD/CAD',
      'USD/CHF',
      'USD/JPY',
    ]);
  });

  it('parses a single USD/CAD card chunk with arrow direction', () => {
    const chunk = `
      USD/CAD  30 MIN  12:14 AM (UTC-5)
      Friday, July 10, 2026 7:14:53 AM CET
      Expected Move  ↓ 19 - 39 PIPS
      Target  1.4115
      Pivot  1.4170
    `;
    const idea = parseTradingCentralCardChunk(chunk, ctx);
    expect(idea).not.toBeNull();
    expect(idea!.instrument).toBe('USD/CAD');
    expect(idea!.direction).toBe('DOWN');
    expect(idea!.expectedMovePips).toEqual([19, 39]);
    expect(idea!.target).toBe(1.4115);
    expect(idea!.pivot).toBe(1.417);
  });

  it('rejects shell chrome with no Target/Pivot/PIPS', () => {
    const idea = parseTradingCentralCardChunk(
      'USD/CAD something unrelated without levels',
      ctx,
    );
    expect(idea).toBeNull();
  });

  it('TradingCentralExtractor.extractFromPlainText drives the shipped parser', () => {
    const extractor = new TradingCentralExtractor();
    const ideas = extractor.extractFromPlainText(liveText, ctx);
    expect(ideas.length).toBe(6);
    expect(ideas.every((i) => i.provider === 'TRADING_CENTRAL')).toBe(true);
  });

  it('TradingCentralExtractor.extractFromDomHtml uses text parser on HTML-ish content', () => {
    const extractor = new TradingCentralExtractor();
    // Simulate iframe HTML that only exposes text (no data- attrs)
    const html = `
      <div class="widget">
        <div>USD/CAD 30 MIN</div>
        <div>Expected Move ↓ 19 - 39 PIPS</div>
        <div>Target 1.4115</div>
        <div>Pivot 1.4170</div>
        <button>Trade</button>
        <div>AUD/USD 30 MIN</div>
        <div>Expected Move ↑ 17 - 32 PIPS</div>
        <div>Target 0.6985</div>
        <div>Pivot 0.6940</div>
        <button>Trade</button>
      </div>
    `;
    const ideas = extractor.extractFromDomHtml(html, ctx);
    expect(ideas.length).toBe(2);
    expect(ideas.map((i) => i.instrument).sort()).toEqual(['AUD/USD', 'USD/CAD']);
  });
});
