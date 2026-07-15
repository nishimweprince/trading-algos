import { mkdtempSync, rmSync, writeFileSync } from 'fs';
import { tmpdir } from 'os';
import { join } from 'path';
import { AppConfigService } from '../src/config/app-config.service';
import {
  OpenAiVisionError,
  OpenAiVisionService,
  TradingCentralVisionPayload,
} from '../src/openai/openai-vision.service';
import { normalizeVisionInstrument } from '../src/scraper/extractors/normalize';
import { ExtractContext } from '../src/scraper/idea-extractor.interface';

describe('OpenAiVisionService', () => {
  let dir: string;
  let screenshotPath: string;
  let parse: jest.Mock;
  let service: OpenAiVisionService;

  const context: ExtractContext = {
    provider: 'TRADING_CENTRAL',
    sourceUrl: 'https://secure.ic.com/TradingCentral/TradingCentral',
    capturedAt: '2026-07-14T04:00:00.000Z',
  };

  const validPayload: TradingCentralVisionPayload = {
    signals: [
      {
        instrument: 'aud/jpy',
        timeframe: '30 min',
        direction: 'UP',
        entry: 112.47,
        pivot: 112.17,
        target: 113.06,
        expectedMovePips: [40, 59],
        ideaTimestampText: '2026-07-14T06:02:34+01:00',
        rawSourceText: 'AUD/JPY 30 MIN Target 113.06 Pivot 112.17',
      },
    ],
    rejected: [],
  };

  beforeEach(() => {
    dir = mkdtempSync(join(tmpdir(), 'openai-vision-'));
    screenshotPath = join(dir, 'tc.png');
    writeFileSync(screenshotPath, Buffer.from('fake-png-bytes'));

    const config = new AppConfigService();
    config.load({
      SOURCES: JSON.stringify([
        { type: 'TRADING_CENTRAL', url: context.sourceUrl },
      ]),
      BROWSER_MODE: 'CDP',
      OPENAI_API_KEY: 'test-openai-key',
      OPENAI_MODEL: 'gpt-5.6-luna',
      OPENAI_TIMEOUT_MS: '61000',
    });
    parse = jest.fn();
    service = new OpenAiVisionService(
      config,
      { parse } as unknown as ConstructorParameters<
        typeof OpenAiVisionService
      >[1],
    );
  });

  afterEach(() => {
    rmSync(dir, { recursive: true, force: true });
  });

  function respond(payload: TradingCentralVisionPayload): void {
    parse.mockResolvedValue({
      output_text: JSON.stringify(payload),
      output: [],
      output_parsed: payload,
    });
  }

  it('sends the PNG as Base64 with original detail and structured output', async () => {
    respond(validPayload);

    await service.extract(screenshotPath, {
      ...context,
      screenshotPath,
    });

    expect(parse).toHaveBeenCalledTimes(1);
    const request = parse.mock.calls[0][0];
    expect(request).toMatchObject({
      model: 'gpt-5.6-luna',
      store: false,
      input: [
        {
          role: 'user',
          content: [
            { type: 'input_text' },
            {
              type: 'input_image',
              detail: 'original',
              image_url: `data:image/png;base64,${Buffer.from('fake-png-bytes').toString('base64')}`,
            },
          ],
        },
      ],
      text: {
        format: {
          type: 'json_schema',
          name: 'trading_central_extraction',
          strict: true,
        },
      },
    });

    const prompt = request.input[0].content[0].text as string;
    expect(prompt).toContain('BLACK/DARK price tag');
    expect(prompt).toContain('labeled "Target" row BELOW the chart');
    expect(prompt).toContain('labeled "Pivot" row BELOW the Target row');
    expect(prompt).toContain('Preserve the decimal point position');
    expect(prompt).toContain('Never move a decimal point');
    expect(prompt).toContain(
      'entry=1.3401, target=1.3440, pivot=1.3370',
    );
    expect(prompt).toContain(
      'entry=162.19, target=161.60, pivot=162.30',
    );

    const expectedMoveArraySchema =
      request.text.format.schema.properties.signals.items.properties
        .expectedMovePips.anyOf[0];
    expect(expectedMoveArraySchema).toMatchObject({
      type: 'array',
      items: { type: 'number' },
      minItems: 2,
      maxItems: 2,
    });
    expect(Array.isArray(expectedMoveArraySchema.items)).toBe(false);
  });

  it('maps extracted values, aliases, timestamp, and stable hash locally', async () => {
    respond(validPayload);

    const first = await service.extract(screenshotPath, {
      ...context,
      screenshotPath,
    });
    const second = await service.extract(screenshotPath, {
      ...context,
      screenshotPath,
    });

    expect(first.ideas).toHaveLength(1);
    expect(first.ideas[0]).toMatchObject({
      instrument: 'AUD/JPY',
      timeframe: '30 MIN',
      direction: 'UP',
      entry: 112.47,
      pivot: 112.17,
      stopLoss: 112.17,
      target: 113.06,
      takeProfit: 113.06,
      expectedMovePips: [40, 59],
      ideaTimestamp: '2026-07-14T05:02:34.000Z',
    });
    expect(first.ideas[0].hash).toBe(second.ideas[0].hash);
  });

  it('allows nullable entry and timestamp without rejecting a complete card', async () => {
    respond({
      signals: [
        {
          ...validPayload.signals[0],
          entry: null,
          ideaTimestampText: null,
        },
      ],
      rejected: [],
    });

    const result = await service.extract(screenshotPath, context);

    expect(result.ideas[0]).toMatchObject({
      entry: null,
      ideaTimestamp: null,
    });
  });

  it('rejects incomplete and directionally contradictory cards', async () => {
    respond({
      signals: [
        { ...validPayload.signals[0], pivot: null },
        {
          ...validPayload.signals[0],
          direction: 'DOWN',
          pivot: 112.17,
          target: 113.06,
        },
      ],
      rejected: [],
    });

    const result = await service.extract(screenshotPath, context);

    expect(result.ideas).toHaveLength(0);
    expect(result.rejected[0].reasons).toContain('missing Pivot/stopLoss');
    expect(result.rejected[1].reasons).toContain(
      'DOWN direction contradicts Target/Pivot relationship',
    );
  });

  it('fails before an API request when the screenshot is missing', async () => {
    await expect(
      service.extract(join(dir, 'missing.png'), context),
    ).rejects.toMatchObject({ stage: 'validation' } satisfies Partial<OpenAiVisionError>);
    expect(parse).not.toHaveBeenCalled();
  });

  it('classifies API failures and redacts the configured key', async () => {
    parse.mockRejectedValue(
      new Error('request failed with test-openai-key in diagnostic text'),
    );

    await expect(service.extract(screenshotPath, context)).rejects.toMatchObject({
      stage: 'openai',
      message: expect.not.stringContaining('test-openai-key'),
    });
  });

  it('classifies a missing parsed payload as validation failure', async () => {
    parse.mockResolvedValue({
      output_text: '',
      output: [{ type: 'message', content: [{ type: 'refusal' }] }],
      output_parsed: null,
    });

    await expect(service.extract(screenshotPath, context)).rejects.toMatchObject({
      stage: 'validation',
      model: 'gpt-5.6-luna',
    });
  });

  it.each([
    ['GBPIUSD', 'GBP/USD'],
    ['USDICHF', 'USD/CHF'],
    ['USDIJPY', 'USD/JPY'],
    ['XAUIUSD', 'XAU/USD'],
  ])('normalizes the Windows screenshot pair artifact %s', (raw, expected) => {
    expect(normalizeVisionInstrument(raw, 'WINDOWS')).toBe(expected);
  });

  it('does not rewrite pairs on macOS or unknown assets', () => {
    expect(normalizeVisionInstrument('GBPIUSD', 'MACOS')).toBe('GBPIUSD');
    expect(normalizeVisionInstrument('ABCIDEF', 'WINDOWS')).toBe('ABCIDEF');
    expect(normalizeVisionInstrument('GBP/USD', 'WINDOWS')).toBe('GBP/USD');
  });
});
