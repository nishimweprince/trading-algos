import { Injectable, Logger } from '@nestjs/common';
import { readFile } from 'fs/promises';
import { Ollama } from 'ollama';
import { z } from 'zod';
import { AppConfigService } from '../config/app-config.service';
import { detectHostOs, ResolvedHostOs } from '../config/sources.schema';
import { computeIdeaHash } from '../dedup/hash';
import { Direction, TradingIdea } from '../models/trading-idea.model';
import { OcrResult } from '../ocr/ocr.service';
import { ExtractContext } from '../scraper/idea-extractor.interface';
import { normalizeDirection, toIsoTimestamp } from '../scraper/extractors/normalize';

const NullableNumberSchema = z.preprocess((value) => {
  if (value === null || value === undefined || value === '') return null;
  if (typeof value === 'string') return Number(value.replace(/,/g, ''));
  return value;
}, z.number().finite().nullable());

const RawSignalSchema = z
  .object({
    instrument: z.string().nullable().optional(),
    timeframe: z.string().nullable().optional(),
    direction: z.string().nullable().optional(),
    entry: NullableNumberSchema.optional().default(null),
    stopLoss: NullableNumberSchema.optional().default(null),
    takeProfit: NullableNumberSchema.optional().default(null),
    ideaTimestamp: z.string().nullable().optional().default(null),
    expectedMovePips: z
      .array(z.number().finite())
      .length(2)
      .nullable()
      .optional()
      .default(null),
    support: z.array(z.number().finite()).optional().default([]),
    resistance: z.array(z.number().finite()).optional().default([]),
    sourceText: z.string().optional(),
  })
  .passthrough();

const FormatterPayloadSchema = z.object({
  signals: z.array(RawSignalSchema),
  rejected: z
    .array(
      z.object({
        source: z.string().optional(),
        reasons: z.array(z.string()).min(1),
      }),
    )
    .optional()
    .default([]),
});

const PAIR_ASSET_CODES = new Set([
  'USD',
  'EUR',
  'GBP',
  'JPY',
  'CHF',
  'CAD',
  'AUD',
  'NZD',
  'XAU',
  'XAG',
  'BTC',
  'ETH',
  'LTC',
  'XRP',
  'BCH',
]);

/** Correct the Windows OCR artifact where a currency-pair slash is read as I. */
export function normalizeOcrInstrument(
  raw: string | null | undefined,
  hostOs: ResolvedHostOs | undefined,
): string {
  const normalized = raw?.trim().toUpperCase() ?? '';
  if (hostOs !== 'WINDOWS') return normalized;

  const compact = normalized.replace(/\s+/g, '');
  const mistakenSeparator = compact.match(/^([A-Z]{3})I([A-Z]{3})$/);
  if (!mistakenSeparator) return normalized;

  const [, base, quote] = mistakenSeparator;
  if (!PAIR_ASSET_CODES.has(base) || !PAIR_ASSET_CODES.has(quote)) {
    return normalized;
  }
  return `${base}/${quote}`;
}

export interface RejectedSignal {
  source?: string;
  reasons: string[];
}

export interface FormattedSignalsResult {
  ideas: TradingIdea[];
  rejected: RejectedSignal[];
  model: string;
  rawResponse: string;
  repaired: boolean;
}

export class OllamaFormatterError extends Error {
  constructor(
    public readonly stage: 'ollama' | 'validation',
    message: string,
    public readonly model: string,
    public readonly rawResponse?: string,
  ) {
    super(message);
    this.name = 'OllamaFormatterError';
  }
}

@Injectable()
export class OllamaFormatterService {
  private readonly logger = new Logger(OllamaFormatterService.name);
  private client: Ollama | null = null;

  constructor(private readonly config: AppConfigService) {}

  async format(
    ocr: OcrResult,
    ctx: ExtractContext,
  ): Promise<FormattedSignalsResult> {
    const model = this.config.ollamaModel;
    const prompt = this.buildPrompt(ocr);
    const image = await this.loadScreenshotImage(ctx.screenshotPath);
    let rawResponse: string;
    try {
      rawResponse = await this.callModel(prompt, image);
    } catch (err) {
      throw new OllamaFormatterError(
        'ollama',
        `Ollama request failed: ${err instanceof Error ? err.message : String(err)}`,
        model,
      );
    }

    let payload: z.infer<typeof FormatterPayloadSchema>;
    let repaired = false;
    try {
      payload = this.parsePayload(rawResponse);
    } catch (firstError) {
      repaired = true;
      const repairPrompt = [
        'Repair the response below into the exact JSON contract included here.',
        'Return JSON only. Do not add or infer values.',
        `Validation error: ${firstError instanceof Error ? firstError.message : String(firstError)}`,
        'ORIGINAL CONTRACT AND OCR:',
        prompt,
        'INVALID RESPONSE:',
        rawResponse,
      ].join('\n');
      try {
        rawResponse = await this.callModel(repairPrompt, image);
        payload = this.parsePayload(rawResponse);
      } catch (err) {
        throw new OllamaFormatterError(
          'validation',
          `Ollama response failed JSON validation after one repair: ${err instanceof Error ? err.message : String(err)}`,
          model,
          rawResponse,
        );
      }
    }

    const rejected: RejectedSignal[] = [...payload.rejected];
    const ideas: TradingIdea[] = [];
    const configuredHostOs = this.config.hostOs;
    const hostOs =
      configuredHostOs === 'AUTO' ? detectHostOs() : configuredHostOs;
    for (const candidate of payload.signals) {
      const reasons: string[] = [];
      const rawInstrument = candidate.instrument?.trim().toUpperCase() ?? '';
      const instrument = normalizeOcrInstrument(rawInstrument, hostOs);
      if (instrument !== rawInstrument) {
        this.logger.log(
          `Normalized Windows OCR instrument ${rawInstrument} -> ${instrument}`,
        );
      }
      const timeframe = candidate.timeframe?.trim().toUpperCase() ?? '';
      const direction = normalizeDirection(candidate.direction);
      if (!instrument) reasons.push('missing instrument');
      if (!timeframe) reasons.push('missing timeframe');
      if (direction === 'NEUTRAL') reasons.push('missing or neutral direction');
      if (candidate.stopLoss == null) reasons.push('missing stopLoss/Pivot');
      if (candidate.takeProfit == null) reasons.push('missing takeProfit/Target');
      if (reasons.length > 0) {
        rejected.push({ source: candidate.sourceText, reasons });
        continue;
      }

      const ideaTimestamp = candidate.ideaTimestamp
        ? toIsoTimestamp(candidate.ideaTimestamp, '') || null
        : null;
      const expectedMovePips = candidate.expectedMovePips
        ? ([candidate.expectedMovePips[0], candidate.expectedMovePips[1]] as [
            number,
            number,
          ])
        : undefined;
      const base = {
        provider: 'TRADING_CENTRAL' as const,
        instrument,
        timeframe,
        direction: direction as Exclude<Direction, 'NEUTRAL'>,
        entry: candidate.entry,
        stopLoss: candidate.stopLoss as number,
        takeProfit: candidate.takeProfit as number,
        pivot: candidate.stopLoss as number,
        target: candidate.takeProfit as number,
        expectedMovePips,
        levels:
          candidate.support.length > 0 || candidate.resistance.length > 0
            ? {
                support: candidate.support,
                resistance: candidate.resistance,
              }
            : undefined,
        ideaTimestamp,
        capturedAt: ctx.capturedAt,
        sourceUrl: ctx.sourceUrl,
        screenshotPath: ctx.screenshotPath,
        raw: { sourceText: candidate.sourceText },
      };
      ideas.push({ ...base, hash: computeIdeaHash(base) });
    }

    this.logger.log(
      `Ollama formatted ${ideas.length} valid signal(s), rejected=${rejected.length}`,
    );
    return { ideas, rejected, model, rawResponse, repaired };
  }

  protected async callModel(
    prompt: string,
    image?: Uint8Array,
  ): Promise<string> {
    const systemContent = image
      ? 'You convert Trading Central chart screenshots into strict JSON. The attached image is ground truth: use it to read exact digits, decimal points, and currency-pair symbols, since the OCR text below may contain misreads. Never invent, estimate, interpolate, or correct a price that is not visibly present in the image.'
      : 'You convert Trading Central OCR text into strict JSON. Never invent, estimate, interpolate, or correct a price that is not present in the OCR input.';
    const response = await this.getClient().chat({
      model: this.config.ollamaModel,
      stream: false,
      think: false,
      options: { temperature: 0 },
      messages: [
        { role: 'system', content: systemContent },
        {
          role: 'user',
          content: prompt,
          ...(image ? { images: [image] } : {}),
        },
      ],
    });
    return response.message.content;
  }

  private async loadScreenshotImage(
    screenshotPath: string | undefined,
  ): Promise<Uint8Array | undefined> {
    if (!screenshotPath) return undefined;
    try {
      return await readFile(screenshotPath);
    } catch (err) {
      this.logger.warn(
        `Unable to read screenshot for vision formatting, falling back to OCR-text-only: ${screenshotPath} (${err instanceof Error ? err.message : String(err)})`,
      );
      return undefined;
    }
  }

  private parsePayload(raw: string): z.infer<typeof FormatterPayloadSchema> {
    const cleaned = raw
      .trim()
      .replace(/^```(?:json)?\s*/i, '')
      .replace(/\s*```$/, '');
    return FormatterPayloadSchema.parse(JSON.parse(cleaned));
  }

  private buildPrompt(ocr: OcrResult): string {
    const plain = ocr.text.slice(0, 120_000);
    const positioned = ocr.positionalText.slice(0, 120_000);
    return [
      'Extract every complete Trading Central signal card from the active market tab.',
      'Field rules:',
      '- The unlabeled black chart price marker is entry. Use null if it cannot be associated confidently.',
      '- Pivot is stopLoss.',
      '- Target is takeProfit.',
      '- Direction must be UP or DOWN. Use the expected-move arrow and price relationships only when explicitly visible.',
      '- Do not switch cards, merge columns, or infer missing digits/decimals.',
      '- Put incomplete/ambiguous cards in rejected with reasons.',
      'Return exactly this JSON shape with no markdown:',
      '{"signals":[{"instrument":"AUD/JPY","timeframe":"30 MIN","direction":"UP","entry":112.47,"stopLoss":112.17,"takeProfit":113.06,"ideaTimestamp":"2026-07-14T06:02:34+01:00","expectedMovePips":[40,59],"support":[],"resistance":[],"sourceText":"relevant OCR fragment"}],"rejected":[{"source":"fragment","reasons":["reason"]}]}',
      'Use null for optional entry, ideaTimestamp, and expectedMovePips. signals must require instrument, timeframe, direction, stopLoss, and takeProfit.',
      `OCR mean confidence: ${ocr.confidence}`,
      'PLAIN OCR:',
      plain,
      'POSITIONAL OCR LINES:',
      positioned,
    ].join('\n');
  }

  private getClient(): Ollama {
    if (this.client) return this.client;
    const timeoutMs = this.config.ollamaTimeoutMs;
    const baseFetch = globalThis.fetch.bind(globalThis);
    const timedFetch: typeof fetch = async (input, init) => {
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), timeoutMs);
      const originalSignal = init?.signal;
      const abort = () => controller.abort();
      originalSignal?.addEventListener('abort', abort, { once: true });
      try {
        return await baseFetch(input, { ...init, signal: controller.signal });
      } finally {
        clearTimeout(timer);
        originalSignal?.removeEventListener('abort', abort);
      }
    };
    this.client = new Ollama({
      host: this.config.ollamaHost,
      headers: { Authorization: `Bearer ${this.config.ollamaApiKey}` },
      fetch: timedFetch,
    });
    return this.client;
  }
}
