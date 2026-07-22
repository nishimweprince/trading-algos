import { Injectable, Logger, Optional } from '@nestjs/common';
import { readFile } from 'fs/promises';
import OpenAI from 'openai';
import { zodTextFormat } from 'openai/helpers/zod';
import { z } from 'zod';
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
      stopLoss: NullableNumberSchema,
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

function mirrorStopLossOneToOne(entry: number, target: number): number {
  return 2 * entry - target;
}

function isOneToOneStopLoss(
  entry: number,
  target: number,
  stopLoss: number,
): boolean {
  const expected = mirrorStopLossOneToOne(entry, target);
  const tolerance = Math.max(Math.abs(entry), Math.abs(target), 1) * 1e-6;
  return Math.abs(stopLoss - expected) <= tolerance;
}

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

Card boundaries and reading order:
1. Process one card at a time, from its "SYMBOL TIMEFRAME" header through the description paragraph and the Expiry line beneath the same chart.
2. Keep cards separate. Never copy or merge a value from a neighboring card, column, or the price axis of a different chart.

Fields to read from each card:
3. instrument: the symbol in the card header, exactly as shown (e.g. "GBPDKK", "USDZAR", "EURGBP"). Do not insert spaces or slashes.
4. timeframe: the number shown next to the symbol in the header (e.g. "GBPDKK 30" -> timeframe "30"). It is the chart interval in minutes.
5. pattern: the bold pattern title above the description (e.g. "Channel Down Emerging", "Resistance Emerging", "Channel Up Emerging").
6. direction: read the description sentence "Possible <bullish|bearish> price movement". "bullish" -> UP, "bearish" -> DOWN. Use null if neither word is present.
7. target: the price the description says price is moving "towards" (e.g. "towards the resistance 8.7889" -> 8.7889, "towards the support 0.8503" -> 0.8503). Read every digit and keep the decimal point exactly. This is the take-profit level.
8. currentPrice: the latest/current market price of THIS chart. Read the price on the right-hand price axis that is level with the most recent (right-most) candle. If it is not clearly readable, return null. This is the entry price.
9. stopLoss: after target and currentPrice are identified, set stop loss at a 1:1 risk-reward ratio relative to entry. The stop must be the same distance from currentPrice as target is, but on the opposite side. Formula: stopLoss = 2 * currentPrice - target. Example (bullish): currentPrice 16.5, target 16.6 -> stopLoss 16.4. Example (bearish): currentPrice 1.0900, target 1.0800 -> stopLoss 1.1000. Autochartist cards usually do not show a stop level; compute stopLoss with this rule even when no stop is visible. If a visible chart level would imply a different stop distance, ignore it and use the 1:1 stopLoss instead.
10. forecastHorizon: the "within the next ..." phrase (e.g. "1 day", "8 hours"). Use null if absent.
11. identifiedAtText: the "identified at <M/D HH:MM>" time from the description, verbatim. Use null if unreadable.
12. expiryText: the "Expiry Date/Time: <...>" value, verbatim. Use null if absent.
13. rawSourceText: the visible text supporting this card, including the pattern title, the description sentence, and the target price.

Accuracy and rejection rules:
14. Read currentPrice, target, and stopLoss independently, digit by digit. Never move a decimal point, round, append a zero, drop a digit, or infer an unreadable digit.
15. A bullish (UP) card should have target above currentPrice and stopLoss below currentPrice; a bearish (DOWN) card should have target below currentPrice and stopLoss above currentPrice. If the readable values contradict the direction, still return what you read and explain the conflict in rejected.
16. If target or currentPrice is cut off or unreadable, return null for that field and record the problem in rejected.
17. Ignore navigation tabs, "Trade Now" buttons, the RSI subchart, account controls, and any card cut off beyond reliable extraction.
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
   * Autochartist "Our Favourites" vision path. Cards give a target level and
   * direction but no stop-loss, so the stop-loss is a risk-reward mirror of the
   * target across the current price (1:1 R:R): stopLoss = 2 * entry - target.
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

      // Risk-reward mirror: reflect the target across the entry (1:1 R:R).
      const stopLoss =
        candidate.stopLoss != null &&
        isOneToOneStopLoss(entry as number, target as number, candidate.stopLoss)
          ? candidate.stopLoss
          : mirrorStopLossOneToOne(entry as number, target as number);
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
