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
  normalizeVisionInstrument,
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
    const model = this.config.openaiModel;
    let image: Buffer;
    try {
      image = await readFile(screenshotPath);
    } catch (err) {
      throw new OpenAiVisionError(
        'validation',
        `Unable to read Trading Central screenshot: ${this.safeErrorMessage(err)}`,
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
              { type: 'input_text', text: EXTRACTION_PROMPT },
              {
                type: 'input_image',
                image_url: imageUrl,
                detail: 'original',
              },
            ],
          },
        ],
        text: {
          format: zodTextFormat(
            TradingCentralVisionSchema,
            'trading_central_extraction',
          ),
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
    const payload = response.output_parsed as
      | TradingCentralVisionPayload
      | null
      | undefined;
    if (!payload) {
      throw new OpenAiVisionError(
        'validation',
        'OpenAI vision response did not contain parsed structured output',
        model,
        rawResponse,
      );
    }

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
