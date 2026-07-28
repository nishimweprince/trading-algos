import { Injectable, Logger, Optional } from '@nestjs/common';
import { readFile } from 'fs/promises';
import OpenAI from 'openai';
import { zodTextFormat } from 'openai/helpers/zod';
import { z } from 'zod';
import { autochartistStopLoss } from '../autochartist/autochartist-stop-loss';
import { AppConfigService } from '../config/app-config.service';
import { detectHostOs } from '../config/sources.schema';
import { computeIdeaHash } from '../dedup/hash';
import { TradingIdea } from '../models/trading-idea.model';
import { ExtractContext } from '../scraper/idea-extractor.interface';
import {
  autochartistIdeaTimestamp,
  normalizeVisionInstrument,
  splitPairSymbol,
  toIsoTimestamp,
} from '../scraper/extractors/normalize';

const NullableNumberSchema = z.number().finite().nullable();

export const TradingCentralVisionSchema = z.object({
  signals: z.array(
    z.object({
      instrument: z.string().nullable(),
      timeframe: z.string().nullable(),
      direction: z.enum(['UP', 'DOWN']).nullable(),
      entry: NullableNumberSchema,
      pivot: NullableNumberSchema,
      target: NullableNumberSchema,
      expectedMovePips: z
        .array(z.number().finite())
        .length(2)
        .nullable(),
      ideaTimestampText: z.string().nullable(),
      rawSourceText: z.string().nullable(),
    }),
  ),
  rejected: z.array(
    z.object({
      source: z.string().nullable(),
      reasons: z.array(z.string()).min(1),
    }),
  ),
});

export type TradingCentralVisionPayload = z.infer<
  typeof TradingCentralVisionSchema
>;

export const AutochartistVisionSchema = z.object({
  signals: z.array(
    z.object({
      instrument: z.string().nullable(),
      timeframe: z.string().nullable(),
      pattern: z.string().nullable(),
      direction: z.enum(['UP', 'DOWN']).nullable(),
      currentPrice: NullableNumberSchema,
      target: NullableNumberSchema,
      forecastHorizon: z.string().nullable(),
      identifiedAtText: z.string().nullable(),
      expiryText: z.string().nullable(),
      rawSourceText: z.string().nullable(),
    }),
  ),
  rejected: z.array(
    z.object({
      source: z.string().nullable(),
      reasons: z.array(z.string()).min(1),
    }),
  ),
});

export type AutochartistVisionPayload = z.infer<
  typeof AutochartistVisionSchema
>;

export interface RejectedVisionSignal {
  source?: string;
  reasons: string[];
}

export interface OpenAiExtractionResult {
  ideas: TradingIdea[];
  rejected: RejectedVisionSignal[];
  model: string;
  rawResponse: string;
}

export class OpenAiVisionError extends Error {
  constructor(
    public readonly stage: 'openai' | 'validation',
    message: string,
    public readonly model: string,
    public readonly rawResponse?: string,
  ) {
    super(message);
    this.name = 'OpenAiVisionError';
  }
}

type ResponsesClient = Pick<OpenAI['responses'], 'parse'>;

const EXTRACTION_PROMPT = `
Extract every fully readable Trading Central signal card from this screenshot.

Card boundaries and reading order:
1. Process one card at a time from its instrument/timeframe header through the labeled rows beneath that same chart.
2. Keep cards separate. Never copy or merge a value from a neighboring column, row, or partially visible card.

Authoritative price mapping for each card:
3. entry: read the number inside the BLACK/DARK price tag on the right edge of the chart. That tag is attached to a thin black horizontal price line and represents the entry/current market price. Do not use a blue Pivot tag, a green/red target or support/resistance tag, a candle value, an axis label, or a chart border as entry.
4. take profit: read the value from the labeled "Target" row BELOW the chart. Return that value as target. The bottom labeled row is authoritative even when a matching colored level also appears inside the chart.
5. stop loss: read the value from the labeled "Pivot" row BELOW the Target row at the bottom of the card. Return that value as pivot. Pivot is the stop loss; do not substitute the black entry price or another chart level.
6. Direction is UP only when the card shows an upward idea and Target is above Pivot. Direction is DOWN only when the card shows a downward idea and Target is below Pivot.

Decimal and digit accuracy:
7. Read entry, Target, and Pivot independently, character by character, from their authoritative labels. Preserve the decimal point position and every visible digit.
8. Before returning a price, visually verify it a second time against the same label. Pay special attention to small decimal points and similar glyphs such as 0/6/8, 1/7, and 3/5.
9. Never move a decimal point, round a price, change its precision, append a zero, drop a digit, or infer a missing digit from typical forex precision.
10. Example of the mapping only: black tag 1.3401 + bottom Target 1.3440 + bottom Pivot 1.3370 means entry=1.3401, target=1.3440, pivot=1.3370.
11. JPY example of the mapping only: black tag 162.19 + bottom Target 161.60 + bottom Pivot 162.30 means entry=162.19, target=161.60, pivot=162.30.

Other fields and rejection rules:
12. Instrument must be the visible AAA/BBB market symbol and timeframe must come from the same card header.
13. Preserve the displayed signal timestamp verbatim in ideaTimestampText. Use null if unreadable.
14. expectedMovePips is the displayed two-number Expected Move range from the same card. Use null if unreadable.
15. rawSourceText should contain the visible text supporting that signal, including the three authoritative prices when readable.
16. Never invent, estimate, interpolate, repair, or borrow an unreadable character or price.
17. If Target or Pivot is cut off or unreadable, include the visible candidate with null for that field and explain the problem in rejected. If the black entry tag is unreadable, entry may be null without rejecting an otherwise complete card.
18. Ignore navigation, account controls, Trade buttons, and cards cut off beyond reliable extraction.
`.trim();

const AUTOCHARTIST_EXTRACTION_PROMPT = `
Extract every fully readable Autochartist "Our Favourites" trade-setup card from this screenshot.

This is the IC Markets / webapp.autochartist.com grid layout. Each card shows a header row, pattern name, metadata row, candlestick chart, and an RSI subchart. These cards do NOT include a long description paragraph or an "Expiry Date/Time" line — do not reject a card for missing those fields.

Card boundaries and reading order:
1. Process one card at a time from its "SYMBOL TIMEFRAME" header through the chart area in the same bordered card.
2. Keep cards separate. Never copy or merge a value from a neighboring card, column, or the price axis of a different chart.

Fields to read from each card:
3. instrument: the symbol in the card header, exactly as shown (e.g. "EURTRY", "CHFJPY", "AUDUSD"). Do not insert spaces or slashes.
4. timeframe: the number shown next to the symbol in the header (e.g. "EURTRY 60" -> timeframe "60", "AUDUSD 240" -> "240"). It is the chart interval in minutes.
5. pattern: the pattern title directly under the header (e.g. "Channel Up", "Channel Down", "Rising Wedge", "Triangle").
6. target: read the labeled "Forecast" price on the metadata row (e.g. "Forecast 53.742" -> 53.742, "Forecast 0.5672" -> 0.5672). This is the take-profit level. Read every digit and keep the decimal point exactly.
7. currentPrice: the latest/current market price of THIS chart. Read the price on the right-hand price axis that is level with the most recent (right-most) candle. The Forecast label may also appear highlighted on that axis — do not confuse the axis label with the last-candle price. If it is not clearly readable, return null. This is the entry price.
8. direction: UP when target is above currentPrice; DOWN when target is below currentPrice. If currentPrice is null but target is readable, infer from the shaded forecast box on the chart (green zone above price -> UP, red zone below price -> DOWN) or from the pattern name ("Channel Up" -> UP, "Channel Down" -> DOWN, "Rising Wedge" -> DOWN). Use null only when direction cannot be determined.
9. forecastHorizon: use null — this layout does not show a "within the next ..." phrase.
10. identifiedAtText: the "Identified at <M/D HH:MM>" value from the metadata row, verbatim (e.g. "7/23 14:00"). Use null if unreadable.
11. expiryText: use null — this layout does not show an expiry line.
12. rawSourceText: the visible text supporting this card: header, pattern, identified-at time, probability percentage, forecast price, and any readable axis prices.

Accuracy and rejection rules:
13. Read currentPrice and target independently, digit by digit. Never move a decimal point, round, append a zero, drop a digit, or infer an unreadable digit. Autochartist cards do not show a stop level — do not return stopLoss.
14. A UP card should have target above currentPrice; a DOWN card should have target below currentPrice. If the readable values contradict the direction, still return what you read and explain the conflict in rejected.
15. If target or currentPrice is cut off or unreadable, return null for that field and record the problem in rejected.
16. Ignore navigation tabs, filter bars, "Trade Now" buttons, the RSI subchart, account controls, and any card cut off beyond reliable extraction.
17. Return a card in signals when instrument, timeframe, direction, target, and currentPrice are all readable — even without description or expiry text.
`.trim();

@Injectable()
export class OpenAiVisionService {
  private readonly logger = new Logger(OpenAiVisionService.name);
  private client: ResponsesClient | null = null;

  constructor(
    private readonly config: AppConfigService,
    @Optional() private readonly suppliedClient?: ResponsesClient,
  ) {}

  async extract(
    screenshotPath: string,
    ctx: ExtractContext,
  ): Promise<OpenAiExtractionResult> {
    const { payload, rawResponse, model } = await this.callVision({
      prompt: EXTRACTION_PROMPT,
      schema: TradingCentralVisionSchema,
      schemaName: 'trading_central_extraction',
      screenshotPath,
      readErrorLabel: 'Trading Central',
    });

    const rejected: RejectedVisionSignal[] = payload.rejected.map((item) => ({
      source: item.source ?? undefined,
      reasons: item.reasons,
    }));
    const ideas: TradingIdea[] = [];
    const configuredHostOs = this.config.hostOs;
    const hostOs =
      configuredHostOs === 'AUTO' ? detectHostOs() : configuredHostOs;

    for (const candidate of payload.signals) {
      const source = candidate.rawSourceText ?? undefined;
      const reasons: string[] = [];
      const rawInstrument = candidate.instrument?.trim().toUpperCase() ?? '';
      const instrument = normalizeVisionInstrument(rawInstrument, hostOs);
      const timeframe = candidate.timeframe?.trim().toUpperCase() ?? '';

      if (!instrument) {
        reasons.push('missing instrument');
      } else if (!/^[A-Z]{3}\/[A-Z]{3}$/.test(instrument)) {
        reasons.push('instrument must use AAA/BBB format');
      }
      if (!timeframe) reasons.push('missing timeframe');
      if (!candidate.direction) reasons.push('missing direction');
      if (candidate.pivot == null) reasons.push('missing Pivot/stopLoss');
      if (candidate.target == null) reasons.push('missing Target/takeProfit');
      if (
        candidate.direction === 'UP' &&
        candidate.pivot != null &&
        candidate.target != null &&
        candidate.target <= candidate.pivot
      ) {
        reasons.push('UP direction contradicts Target/Pivot relationship');
      }
      if (
        candidate.direction === 'DOWN' &&
        candidate.pivot != null &&
        candidate.target != null &&
        candidate.target >= candidate.pivot
      ) {
        reasons.push('DOWN direction contradicts Target/Pivot relationship');
      }

      if (reasons.length > 0) {
        rejected.push({ source, reasons });
        continue;
      }

      const ideaTimestamp = candidate.ideaTimestampText
        ? toIsoTimestamp(candidate.ideaTimestampText, '') || null
        : null;
      const expectedMovePips: [number, number] | undefined =
        candidate.expectedMovePips == null
          ? undefined
          : [candidate.expectedMovePips[0], candidate.expectedMovePips[1]];
      const base = {
        provider: 'TRADING_CENTRAL' as const,
        instrument,
        timeframe,
        direction: candidate.direction as 'UP' | 'DOWN',
        entry: candidate.entry,
        stopLoss: candidate.pivot as number,
        takeProfit: candidate.target as number,
        pivot: candidate.pivot as number,
        target: candidate.target as number,
        expectedMovePips,
        ideaTimestamp,
        capturedAt: ctx.capturedAt,
        sourceUrl: ctx.sourceUrl,
        screenshotPath: ctx.screenshotPath,
        raw: {
          sourceText: candidate.rawSourceText,
          ideaTimestampText: candidate.ideaTimestampText,
        },
      };
      ideas.push({ ...base, hash: computeIdeaHash(base) });
    }

    this.logger.log(
      `OpenAI vision extracted ${ideas.length} valid signal(s), rejected=${rejected.length}`,
    );
    return { ideas, rejected, model, rawResponse };
  }

  /**
   * Autochartist "Our Favourites" vision path. Cards give a forecast target and
   * direction but no stop-loss; stop-loss is derived locally from entry + forecast.
   */
  async extractAutochartist(
    screenshotPath: string,
    ctx: ExtractContext,
  ): Promise<OpenAiExtractionResult> {
    const { payload, rawResponse, model } = await this.callVision({
      prompt: AUTOCHARTIST_EXTRACTION_PROMPT,
      schema: AutochartistVisionSchema,
      schemaName: 'autochartist_extraction',
      screenshotPath,
      readErrorLabel: 'Autochartist',
    });

    const rejected: RejectedVisionSignal[] = payload.rejected.map((item) => ({
      source: item.source ?? undefined,
      reasons: item.reasons,
    }));
    const ideas: TradingIdea[] = [];

    for (const candidate of payload.signals) {
      const source = candidate.rawSourceText ?? undefined;
      const reasons: string[] = [];
      const instrument = splitPairSymbol(candidate.instrument);
      const timeframe = candidate.timeframe?.trim().toUpperCase() ?? '';
      const entry = candidate.currentPrice;
      const target = candidate.target;

      if (!instrument) reasons.push('missing instrument');
      if (!timeframe) reasons.push('missing timeframe');
      if (!candidate.direction) reasons.push('missing direction');
      if (entry == null) reasons.push('missing current price (entry)');
      if (target == null) reasons.push('missing target');
      if (
        candidate.direction === 'UP' &&
        entry != null &&
        target != null &&
        target <= entry
      ) {
        reasons.push('UP direction requires target above current price');
      }
      if (
        candidate.direction === 'DOWN' &&
        entry != null &&
        target != null &&
        target >= entry
      ) {
        reasons.push('DOWN direction requires target below current price');
      }

      if (reasons.length > 0) {
        rejected.push({ source, reasons });
        continue;
      }

      const stopLoss = autochartistStopLoss(entry as number, target as number);
      const ideaTimestamp = autochartistIdeaTimestamp(
        candidate.identifiedAtText,
        ctx.capturedAt,
      );
      const base = {
        provider: 'AUTOCHARTIST' as const,
        instrument,
        timeframe,
        direction: candidate.direction as 'UP' | 'DOWN',
        entry,
        stopLoss,
        takeProfit: target as number,
        pivot: stopLoss,
        target: target as number,
        ideaTimestamp,
        capturedAt: ctx.capturedAt,
        sourceUrl: ctx.sourceUrl,
        screenshotPath: ctx.screenshotPath,
        raw: {
          pattern: candidate.pattern,
          forecastHorizon: candidate.forecastHorizon,
          identifiedAtText: candidate.identifiedAtText,
          expiryText: candidate.expiryText,
          sourceText: candidate.rawSourceText,
        },
      };
      ideas.push({ ...base, hash: computeIdeaHash(base) });
    }

    this.logger.log(
      `OpenAI Autochartist vision extracted ${ideas.length} valid signal(s), rejected=${rejected.length}`,
    );
    return { ideas, rejected, model, rawResponse };
  }

  /**
   * Shared OpenAI Responses vision call: read PNG → base64 data URL → parse
   * with a zod structured-output schema. Errors are classified into the
   * 'validation'/'openai' stages with the API key redacted.
   */
  private async callVision<T>(opts: {
    prompt: string;
    schema: z.ZodType<T>;
    schemaName: string;
    screenshotPath: string;
    readErrorLabel: string;
  }): Promise<{ payload: T; rawResponse: string; model: string }> {
    const model = this.config.openaiModel;
    let image: Buffer;
    try {
      image = await readFile(opts.screenshotPath);
    } catch (err) {
      throw new OpenAiVisionError(
        'validation',
        `Unable to read ${opts.readErrorLabel} screenshot: ${this.safeErrorMessage(err)}`,
        model,
      );
    }

    const imageUrl = `data:image/png;base64,${image.toString('base64')}`;
    let response: Awaited<ReturnType<ResponsesClient['parse']>>;
    try {
      response = await this.getClient().parse({
        model,
        store: false,
        input: [
          {
            role: 'user',
            content: [
              { type: 'input_text', text: opts.prompt },
              {
                type: 'input_image',
                image_url: imageUrl,
                detail: 'original',
              },
            ],
          },
        ],
        text: {
          format: zodTextFormat(opts.schema, opts.schemaName),
        },
      });
    } catch (err) {
      throw new OpenAiVisionError(
        'openai',
        `OpenAI vision request failed: ${this.safeErrorMessage(err)}`,
        model,
      );
    }

    const rawResponse =
      response.output_text || JSON.stringify(response.output ?? []);
    const payload = response.output_parsed as T | null | undefined;
    if (!payload) {
      throw new OpenAiVisionError(
        'validation',
        'OpenAI vision response did not contain parsed structured output',
        model,
        rawResponse,
      );
    }
    return { payload, rawResponse, model };
  }

  private getClient(): ResponsesClient {
    if (this.suppliedClient) return this.suppliedClient;
    if (!this.client) {
      this.client = new OpenAI({
        apiKey: this.config.openaiApiKey,
        timeout: this.config.openaiTimeoutMs,
      }).responses;
    }
    return this.client;
  }

  private safeErrorMessage(err: unknown): string {
    const message = err instanceof Error ? err.message : String(err);
    const apiKey = this.config.openaiApiKey;
    return apiKey ? message.replaceAll(apiKey, '[redacted]') : message;
  }
}
