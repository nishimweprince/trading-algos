import { mkdtempSync, rmSync, writeFileSync } from 'fs';
import { tmpdir } from 'os';
import { join } from 'path';
import { AppConfigService } from '../src/config/app-config.service';
import {
  AutochartistVisionPayload,
  OpenAiVisionError,
  OpenAiVisionService,
} from '../src/openai/openai-vision.service';
import { ExtractContext } from '../src/scraper/idea-extractor.interface';

describe('OpenAiVisionService.extractAutochartist', () => {
  let dir: string;
  let screenshotPath: string;
  let parse: jest.Mock;
  let service: OpenAiVisionService;

  const context: ExtractContext = {
    provider: 'AUTOCHARTIST',
    sourceUrl: 'https://secure.ic.com/AutoChartist/SignIn',
    capturedAt: '2026-07-21T15:05:00.000Z',
  };

  const validPayload: AutochartistVisionPayload = {
    signals: [
      {
        instrument: 'USDZAR',
        timeframe: '15',
        pattern: 'Resistance Emerging',
        direction: 'UP',
        currentPrice: 16.5,
        target: 16.6,
        stopLoss: 16.4,
        forecastHorizon: '8 hours',
        identifiedAtText: '7/21 15:00',
        expiryText: '7/21 23:48',
        rawSourceText:
          'USDZAR 15 Resistance Emerging. Possible bullish price movement towards the resistance 16.6',
      },
    ],
    rejected: [],
  };

  beforeEach(() => {
    dir = mkdtempSync(join(tmpdir(), 'ac-vision-'));
    screenshotPath = join(dir, 'ac.png');
    writeFileSync(screenshotPath, Buffer.from('fake-png-bytes'));

    const config = new AppConfigService();
    config.load({
      SOURCES: JSON.stringify([
        { type: 'AUTOCHARTIST', url: context.sourceUrl },
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

  function respond(payload: AutochartistVisionPayload): void {
    parse.mockResolvedValue({
      output_text: JSON.stringify(payload),
      output: [],
      output_parsed: payload,
    });
  }

  it('sends the PNG as Base64 with the Autochartist structured schema', async () => {
    respond(validPayload);

    await service.extractAutochartist(screenshotPath, {
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
          name: 'autochartist_extraction',
          strict: true,
        },
      },
    });

    const prompt = request.input[0].content[0].text as string;
    expect(prompt).toContain('Our Favourites');
    expect(prompt).toContain('bullish');
    expect(prompt).toContain('1:1 risk-reward');
    expect(prompt).toContain('stopLoss = 2 * currentPrice - target');
  });

  it('maps a card with a risk-reward-mirror stop-loss and stable hash', async () => {
    respond(validPayload);

    const first = await service.extractAutochartist(screenshotPath, {
      ...context,
      screenshotPath,
    });
    // currentPrice drifts between runs; the signal identity must not.
    const second = await service.extractAutochartist(screenshotPath, {
      ...context,
      screenshotPath,
      capturedAt: '2026-07-21T15:20:00.000Z',
    });

    expect(first.ideas).toHaveLength(1);
    const idea = first.ideas[0];
    expect(idea.provider).toBe('AUTOCHARTIST');
    expect(idea.instrument).toBe('USD/ZAR');
    expect(idea.timeframe).toBe('15');
    expect(idea.direction).toBe('UP');
    expect(idea.entry).toBeCloseTo(16.5, 6);
    expect(idea.target).toBeCloseTo(16.6, 6);
    expect(idea.takeProfit).toBeCloseTo(16.6, 6);
    // stopLoss = 2 * entry - target = 2 * 16.5 - 16.6 = 16.4
    expect(idea.stopLoss).toBeCloseTo(16.4, 6);
    expect(idea.pivot).toBeCloseTo(16.4, 6);
    expect(idea.ideaTimestamp).toBe('2026-07-21T15:00:00.000Z');
    expect(idea.hash).toBe(second.ideas[0].hash);
  });

  it('recomputes stop loss at 1:1 when the model returns a non-matching level', async () => {
    respond({
      ...validPayload,
      signals: [{ ...validPayload.signals[0], stopLoss: 16.45 }],
    });

    const result = await service.extractAutochartist(screenshotPath, context);

    expect(result.ideas[0].stopLoss).toBeCloseTo(16.4, 6);
  });

  it('keeps a model stop loss that already matches 1:1 risk-reward', async () => {
    respond(validPayload);

    const result = await service.extractAutochartist(screenshotPath, context);

    expect(result.ideas[0].stopLoss).toBeCloseTo(16.4, 6);
  });

  it('rejects a card missing a readable current price or target', async () => {
    respond({
      signals: [
        { ...validPayload.signals[0], currentPrice: null },
        { ...validPayload.signals[0], target: null },
      ],
      rejected: [],
    });

    const result = await service.extractAutochartist(screenshotPath, context);

    expect(result.ideas).toHaveLength(0);
    expect(result.rejected[0].reasons).toContain('missing current price (entry)');
    expect(result.rejected[1].reasons).toContain('missing target');
  });

  it('rejects a bullish card whose target sits below the current price', async () => {
    respond({
      signals: [
        {
          ...validPayload.signals[0],
          direction: 'UP',
          currentPrice: 16.6,
          target: 16.5,
        },
      ],
      rejected: [],
    });

    const result = await service.extractAutochartist(screenshotPath, context);

    expect(result.ideas).toHaveLength(0);
    expect(result.rejected[0].reasons).toContain(
      'UP direction requires target above current price',
    );
  });

  it('fails before an API request when the screenshot is missing', async () => {
    await expect(
      service.extractAutochartist(join(dir, 'missing.png'), context),
    ).rejects.toMatchObject({
      stage: 'validation',
    } satisfies Partial<OpenAiVisionError>);
    expect(parse).not.toHaveBeenCalled();
  });
});
