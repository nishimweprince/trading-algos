import { AppConfigService } from '../src/config/app-config.service';
import {
  normalizeOcrInstrument,
  OllamaFormatterError,
  OllamaFormatterService,
} from '../src/ollama/ollama-formatter.service';
import { ExtractContext } from '../src/scraper/idea-extractor.interface';

class StubFormatter extends OllamaFormatterService {
  responses: Array<string | Error> = [];
  calls = 0;

  protected override async callModel(): Promise<string> {
    const response = this.responses[this.calls++];
    if (response instanceof Error) throw response;
    return response;
  }
}

describe('OllamaFormatterService', () => {
  const context: ExtractContext = {
    provider: 'TRADING_CENTRAL',
    sourceUrl: 'https://secure.ic.com/TradingCentral/TradingCentral',
    capturedAt: '2026-07-14T04:00:00.000Z',
    screenshotPath: '/tmp/tc.png',
  };
  const ocr = {
    text: 'AUD/JPY 30 MIN 112.47 Target 113.06 Pivot 112.17',
    positionalText: '[x=0,y=0] AUD/JPY 30 MIN',
    confidence: 92,
  };

  function formatter(env: NodeJS.ProcessEnv = {}): StubFormatter {
    const config = new AppConfigService();
    config.load({
      SOURCES: JSON.stringify([
        {
          type: 'TRADING_CENTRAL',
          url: context.sourceUrl,
        },
      ]),
      BROWSER_MODE: 'CDP',
      OLLAMA_API_KEY: 'test-key',
      ...env,
    });
    return new StubFormatter(config);
  }

  it.each([
    ['GBPIUSD', 'GBP/USD'],
    ['USDICHF', 'USD/CHF'],
    ['USDIJPY', 'USD/JPY'],
    ['XAUIUSD', 'XAU/USD'],
  ])('normalizes the Windows OCR pair artifact %s', (raw, expected) => {
    expect(normalizeOcrInstrument(raw, 'WINDOWS')).toBe(expected);
  });

  it('does not rewrite pairs on macOS or unknown three-letter assets', () => {
    expect(normalizeOcrInstrument('GBPIUSD', 'MACOS')).toBe('GBPIUSD');
    expect(normalizeOcrInstrument('ABCIDEF', 'WINDOWS')).toBe('ABCIDEF');
    expect(normalizeOcrInstrument('GBP/USD', 'WINDOWS')).toBe('GBP/USD');
  });

  it('applies Windows pair normalization before creating the idea', async () => {
    const service = formatter({ HOST_OS: 'WINDOWS' });
    service.responses = [
      JSON.stringify({
        signals: [
          {
            instrument: 'GBPIUSD',
            timeframe: '30 MIN',
            direction: 'DOWN',
            stopLoss: 1.338,
            takeProfit: 1.3357,
          },
        ],
        rejected: [],
      }),
    ];

    const result = await service.format(ocr, context);

    expect(result.ideas[0].instrument).toBe('GBP/USD');
  });

  it('maps entry, Pivot/stopLoss, and Target/takeProfit with aliases', async () => {
    const service = formatter();
    service.responses = [
      JSON.stringify({
        signals: [
          {
            instrument: 'aud/jpy',
            timeframe: '30 min',
            direction: 'up',
            entry: 112.47,
            stopLoss: 112.17,
            takeProfit: 113.06,
            ideaTimestamp: null,
            expectedMovePips: [40, 59],
          },
        ],
        rejected: [],
      }),
    ];

    const result = await service.format(ocr, context);
    expect(result.ideas).toHaveLength(1);
    expect(result.ideas[0]).toMatchObject({
      instrument: 'AUD/JPY',
      timeframe: '30 MIN',
      direction: 'UP',
      entry: 112.47,
      stopLoss: 112.17,
      pivot: 112.17,
      takeProfit: 113.06,
      target: 113.06,
      ideaTimestamp: null,
    });
  });

  it('keeps incomplete cards as rejected instead of inventing values', async () => {
    const service = formatter();
    service.responses = [
      JSON.stringify({
        signals: [
          {
            instrument: 'EUR/GBP',
            timeframe: '30 MIN',
            direction: 'UP',
            stopLoss: null,
            takeProfit: 0.8549,
          },
        ],
        rejected: [],
      }),
    ];
    const result = await service.format(ocr, context);
    expect(result.ideas).toHaveLength(0);
    expect(result.rejected[0].reasons).toContain('missing stopLoss/Pivot');
  });

  it('repairs malformed JSON once', async () => {
    const service = formatter();
    service.responses = [
      'not json',
      JSON.stringify({ signals: [], rejected: [] }),
    ];
    const result = await service.format(ocr, context);
    expect(result.repaired).toBe(true);
    expect(service.calls).toBe(2);
  });

  it('reports validation failure after one repair attempt', async () => {
    const service = formatter();
    service.responses = ['not json', 'still not json'];
    await expect(service.format(ocr, context)).rejects.toMatchObject({
      stage: 'validation',
    } satisfies Partial<OllamaFormatterError>);
  });
});
