import { Injectable, Logger, Optional } from '@nestjs/common';
import { Frame, Page } from 'playwright';
import { ProviderType, TradingIdea } from '../../models/trading-idea.model';
import {
  OpenAiExtractionResult,
  OpenAiVisionError,
  OpenAiVisionService,
} from '../../openai/openai-vision.service';
import {
  ExtractContext,
  IdeaExtractor,
} from '../idea-extractor.interface';
import {
  NetworkCaptureSession,
  startNetworkCapture,
} from '../network-capture';
import { findProviderFrame } from '../content-wait';
import {
  normalizeDirection,
  normalizeIdea,
  parseNumber,
  toIsoTimestamp,
} from './normalize';
import { TRADING_CENTRAL_NETWORK_PATTERNS } from './network-patterns';
import { parseTradingCentralText } from './tc-card-parser';

export interface TradingCentralExtractionResult extends OpenAiExtractionResult {}

export class TradingCentralExtractionError extends Error {
  constructor(
    public readonly stage: 'screenshot' | 'openai' | 'validation',
    message: string,
    public readonly details: {
      model?: string;
      rawResponse?: string;
    } = {},
  ) {
    super(message);
    this.name = 'TradingCentralExtractionError';
  }
}

/**
 * Trading Central extractor.
 *
 * IC Markets hosts TC inside:
 *   <iframe id="tradingcentral" src="https://site.recognia.com/icmarketsau/serve.shtml?tkn=...">
 *
 * Primary: network-response interception (incl. recognia.com) started BEFORE navigation.
 * Fallback: DOM scrape of the Recognia iframe (not the host shell page).
 */
@Injectable()
export class TradingCentralExtractor implements IdeaExtractor {
  readonly provider: ProviderType = 'TRADING_CENTRAL';
  readonly networkUrlPatterns = TRADING_CENTRAL_NETWORK_PATTERNS;
  private readonly logger = new Logger(TradingCentralExtractor.name);

  constructor(
    @Optional() private readonly vision?: OpenAiVisionService,
  ) {}

  /**
   * Attach listeners before page.goto. ScraperService owns the lifecycle:
   * begin → navigate → content wait → finish → pass payloads into extract.
   */
  beginNetworkCapture(page: Page): NetworkCaptureSession {
    return startNetworkCapture(page, {
      urlPatterns: this.networkUrlPatterns,
    });
  }

  async extract(page: Page | null, ctx: ExtractContext): Promise<TradingIdea[]> {
    // The live Trading Central path is screenshot → OpenAI vision. Keep the
    // fixture-backed network path below for deterministic legacy unit tests.
    if (
      (!ctx.networkPayloads || ctx.networkPayloads.length === 0) &&
      ctx.screenshotPath &&
      this.vision
    ) {
      return (await this.extractWithDebug(ctx)).ideas;
    }

    // Prefer pre-captured payloads (started before navigation by ScraperService)
    if (ctx.networkPayloads && ctx.networkPayloads.length > 0) {
      const ideas = this.fromNetworkPayloads(ctx.networkPayloads, ctx);
      if (ideas.length > 0) {
        this.logger.log(`Extracted ${ideas.length} idea(s) via network`);
        return ideas;
      }
      this.logger.log(
        `Network payloads present (${ctx.networkPayloads.length}) but no ideas mapped; trying DOM`,
      );
    }

    if (!page) {
      this.logger.warn('No page and no network payloads; returning empty');
      return [];
    }

    // Late XHR residual capture (iframe APIs may still fire after settle)
    this.logger.log('Attempting residual network capture + iframe DOM extraction');
    const session = this.beginNetworkCapture(page);
    try {
      const captured = await session.finish({ settleMs: 2000 });
      if (captured.length > 0) {
        const ideas = this.fromNetworkPayloads(captured, ctx);
        if (ideas.length > 0) {
          this.logger.log(`Extracted ${ideas.length} idea(s) via late network`);
          return ideas;
        }
      }
    } catch (err) {
      this.logger.warn(
        `Late network capture failed: ${err instanceof Error ? err.message : String(err)}`,
      );
      session.dispose();
    }

    this.logger.log('Falling back to DOM extraction (Recognia iframe preferred)');
    return this.fromDom(page, ctx);
  }

  async extractWithDebug(
    ctx: ExtractContext,
  ): Promise<TradingCentralExtractionResult> {
    if (!ctx.screenshotPath) {
      throw new TradingCentralExtractionError(
        'screenshot',
        'Trading Central OCR extraction requires a screenshot path',
      );
    }
    if (!this.vision) {
      throw new TradingCentralExtractionError(
        'openai',
        'Trading Central OpenAI vision service is unavailable',
      );
    }

    try {
      return await this.vision.extract(ctx.screenshotPath, ctx);
    } catch (err) {
      if (err instanceof OpenAiVisionError) {
        throw new TradingCentralExtractionError(err.stage, err.message, {
          model: err.model,
          rawResponse: err.rawResponse,
        });
      }
      throw new TradingCentralExtractionError(
        'openai',
        err instanceof Error ? err.message : String(err),
      );
    }
  }

  /**
   * Parse one or more API payloads into TradingIdea[].
   * Accepts either a flat array of idea objects or nested shapes.
   */
  fromNetworkPayloads(payloads: unknown[], ctx: ExtractContext): TradingIdea[] {
    const ideas: TradingIdea[] = [];
    for (const payload of payloads) {
      const items = this.flattenIdeaItems(payload);
      for (const item of items) {
        const idea = this.mapApiItem(item, ctx);
        if (idea) ideas.push(idea);
      }
    }
    return ideas;
  }

  /**
   * Public pure path for fixture tests — parse API JSON without a browser.
   */
  extractFromApiJson(payload: unknown, ctx: ExtractContext): TradingIdea[] {
    return this.fromNetworkPayloads([payload], ctx);
  }

  /**
   * Public pure path for fixture tests — parse rendered card HTML/text.
   */
  extractFromDomHtml(html: string, ctx: ExtractContext): TradingIdea[] {
    // Prefer structured TC card text parser (matches live Recognia layout)
    const stripped = html
      .replace(/<script[\s\S]*?<\/script>/gi, ' ')
      .replace(/<style[\s\S]*?<\/style>/gi, ' ')
      .replace(/<[^>]+>/g, '\n')
      .replace(/\n+/g, '\n');
    const fromText = parseTradingCentralText(stripped, ctx);
    if (fromText.length > 0) return fromText;
    return this.parseCardsFromHtml(html, ctx);
  }

  /** Public path: parse plain iframe/page body text (live Recognia layout). */
  extractFromPlainText(text: string, ctx: ExtractContext): TradingIdea[] {
    return parseTradingCentralText(text, ctx);
  }

  private flattenIdeaItems(payload: unknown): Record<string, unknown>[] {
    if (payload === null || payload === undefined) return [];
    if (Array.isArray(payload)) {
      return payload.filter(
        (x): x is Record<string, unknown> =>
          typeof x === 'object' && x !== null && !Array.isArray(x),
      );
    }
    if (typeof payload !== 'object') return [];
    const obj = payload as Record<string, unknown>;
    for (const key of [
      'ideas',
      'data',
      'results',
      'items',
      'signals',
      'opinions',
      'stories',
      'products',
      'analyses',
      'analysisList',
    ]) {
      if (Array.isArray(obj[key])) {
        return this.flattenIdeaItems(obj[key]);
      }
      if (obj[key] && typeof obj[key] === 'object') {
        const nested = obj[key] as Record<string, unknown>;
        for (const k2 of [
          'ideas',
          'data',
          'results',
          'items',
          'stories',
          'products',
        ]) {
          if (Array.isArray(nested[k2])) {
            return this.flattenIdeaItems(nested[k2]);
          }
        }
      }
    }
    if (obj.instrument || obj.symbol || obj.pair || obj.name || obj.productName) {
      return [obj];
    }
    return [];
  }

  private mapApiItem(
    item: Record<string, unknown>,
    ctx: ExtractContext,
  ): TradingIdea | null {
    const instrument = String(
      item.instrument ??
        item.symbol ??
        item.pair ??
        item.product ??
        item.productName ??
        item.name ??
        '',
    ).trim();
    if (!instrument) return null;

    const timeframe = String(
      item.timeframe ?? item.term ?? item.period ?? item.horizon ?? item.duration ?? '',
    ).trim() || 'UNKNOWN';

    const direction = normalizeDirection(
      item.direction ??
        item.bias ??
        item.trend ??
        item.side ??
        item.opinion ??
        item.outlook,
    );

    const target = parseNumber(
      item.target ?? item.targetPrice ?? item.tp ?? item.takeProfit ?? item.objective,
    );
    const pivot = parseNumber(
      item.pivot ?? item.pivotPrice ?? item.entry ?? item.alternative,
    );

    let expectedMovePips: [number, number] | undefined;
    if (Array.isArray(item.expectedMovePips) && item.expectedMovePips.length >= 2) {
      const a = parseNumber(item.expectedMovePips[0]);
      const b = parseNumber(item.expectedMovePips[1]);
      if (a !== undefined && b !== undefined) expectedMovePips = [a, b];
    } else if (item.expectedMoveMin != null && item.expectedMoveMax != null) {
      const a = parseNumber(item.expectedMoveMin);
      const b = parseNumber(item.expectedMoveMax);
      if (a !== undefined && b !== undefined) expectedMovePips = [a, b];
    }

    let levels: TradingIdea['levels'];
    if (item.levels && typeof item.levels === 'object') {
      const lv = item.levels as { support?: unknown[]; resistance?: unknown[] };
      levels = {
        support: (lv.support ?? [])
          .map(parseNumber)
          .filter((n): n is number => n !== undefined),
        resistance: (lv.resistance ?? [])
          .map(parseNumber)
          .filter((n): n is number => n !== undefined),
      };
    }

    const ideaTimestamp = toIsoTimestamp(
      item.ideaTimestamp ??
        item.timestamp ??
        item.time ??
        item.updatedAt ??
        item.date ??
        item.publishedAt,
      ctx.capturedAt,
    );

    return normalizeIdea(
      {
        instrument,
        timeframe,
        direction,
        expectedMovePips,
        target,
        pivot,
        levels,
        ideaTimestamp,
        raw: item,
      },
      {
        provider: this.provider,
        sourceUrl: ctx.sourceUrl,
        capturedAt: ctx.capturedAt,
        screenshotPath: ctx.screenshotPath,
      },
    );
  }

  private async fromDom(page: Page, ctx: ExtractContext): Promise<TradingIdea[]> {
    try {
      // Collect text from Recognia iframe first, then every child frame, then main.
      const texts: { label: string; text: string }[] = [];

      const primary =
        findProviderFrame(page, this.provider) ??
        (await this.resolveTradingCentralFrame(page));

      if (primary) {
        this.logger.log(`DOM extract from iframe: ${primary.url()}`);
        const t = await this.readFrameText(primary);
        if (t) texts.push({ label: primary.url(), text: t });
      } else {
        this.logger.warn(
          'iframe#tradingcentral / recognia frame not found; will scan all frames',
        );
      }

      for (const f of page.frames()) {
        if (f === page.mainFrame()) continue;
        if (primary && f === primary) continue;
        const t = await this.readFrameText(f);
        if (t && t.length > 40) texts.push({ label: f.url(), text: t });
      }

      // Main page last (usually just the IC Markets shell)
      try {
        const mainText = await page.locator('body').innerText({ timeout: 5000 });
        if (mainText) texts.push({ label: 'main', text: mainText });
      } catch {
        // ignore
      }

      for (const { label, text } of texts) {
        const ideas = parseTradingCentralText(text, ctx);
        if (ideas.length > 0) {
          this.logger.log(
            `Parsed ${ideas.length} idea(s) from text source: ${label} (${text.length} chars)`,
          );
          return ideas;
        }
      }

      // HTML / evaluate fallbacks per frame
      if (primary) {
        const fromFrame = await this.extractFromFrame(primary, ctx);
        if (fromFrame.length > 0) return fromFrame;
      }
      for (const f of page.frames()) {
        if (f === page.mainFrame()) continue;
        const ideas = await this.extractFromFrame(f, ctx);
        if (ideas.length > 0) {
          this.logger.log(`DOM extract from frame ${f.url()}: ${ideas.length} idea(s)`);
          return ideas;
        }
      }

      // Log a sample so we can diagnose empty extracts without a live session
      const sample = texts.map((t) => t.text).join('\n').slice(0, 500);
      this.logger.warn(
        `No TC ideas parsed. Collected ${texts.length} text source(s). Sample: ${sample.replace(/\s+/g, ' ')}`,
      );

      const mainHtml = await page.content();
      return this.parseCardsFromHtml(mainHtml, ctx);
    } catch (err) {
      this.logger.warn(
        `DOM read failed: ${err instanceof Error ? err.message : String(err)}`,
      );
      return [];
    }
  }

  private async resolveTradingCentralFrame(page: Page): Promise<Frame | null> {
    for (const selector of [
      'iframe#tradingcentral',
      'iframe[src*="recognia.com"]',
      'iframe[src*="tkn="]',
    ]) {
      try {
        const handle = await page.$(selector);
        if (!handle) continue;
        const frame = await handle.contentFrame();
        if (frame) return frame;
      } catch {
        // continue
      }
    }
    return null;
  }

  /** Best-effort plain text from a frame (locator → evaluate → content strip). */
  private async readFrameText(frame: Frame): Promise<string> {
    try {
      const viaLocator = await frame.locator('body').innerText({ timeout: 5000 });
      if (viaLocator && viaLocator.trim().length > 0) return viaLocator;
    } catch {
      // fall through
    }
    try {
      const viaEval = await frame.evaluate(() => {
        const body = document.body;
        if (!body) return '';
        return body.innerText || body.textContent || '';
      });
      if (viaEval && viaEval.trim().length > 0) return viaEval;
    } catch (err) {
      this.logger.warn(
        `frame text evaluate failed (${frame.url()}): ${err instanceof Error ? err.message : String(err)}`,
      );
    }
    try {
      const html = await frame.content();
      return html
        .replace(/<script[\s\S]*?<\/script>/gi, ' ')
        .replace(/<style[\s\S]*?<\/style>/gi, ' ')
        .replace(/<[^>]+>/g, '\n');
    } catch {
      return '';
    }
  }

  private async extractFromFrame(
    frame: Frame,
    ctx: ExtractContext,
  ): Promise<TradingIdea[]> {
    // 0) Plain text path — matches live TC card layout
    const bodyText = await this.readFrameText(frame);
    if (bodyText) {
      const fromText = parseTradingCentralText(bodyText, ctx);
      if (fromText.length > 0) return fromText;
    }

    // 1) HTML attribute / class card parse
    let html = '';
    try {
      html = await frame.content();
    } catch (err) {
      this.logger.warn(
        `frame.content() failed (${frame.url()}): ${err instanceof Error ? err.message : String(err)}`,
      );
    }

    if (html) {
      const embeddedIdeas = this.extractEmbeddedJson(html, ctx);
      if (embeddedIdeas.length > 0) return embeddedIdeas;

      const fromHtml = this.parseCardsFromHtml(html, ctx);
      if (fromHtml.length > 0) return fromHtml;
    }

    // 2) Walk any card-like nodes + parse their text with TC layout parser
    try {
      const cardTexts = await frame.evaluate(() => {
        const out: string[] = [];
        const selectors = [
          '[class*="card"]',
          '[class*="Card"]',
          '[class*="idea"]',
          '[class*="Idea"]',
          '[class*="story"]',
          '[class*="product"]',
          'article',
          'li',
          'section',
        ];
        const seen = new Set<Element>();
        for (const sel of selectors) {
          document.querySelectorAll(sel).forEach((el) => {
            if (seen.has(el)) return;
            const text = (el.textContent || '').replace(/\s+/g, ' ').trim();
            if (text.length < 20 || text.length > 2000) return;
            if (!/\bTarget\b/i.test(text) && !/\bPIPS?\b/i.test(text)) return;
            seen.add(el);
            out.push(text);
          });
        }
        return out;
      });

      const ideas: TradingIdea[] = [];
      const seenHash = new Set<string>();
      for (const text of cardTexts) {
        for (const idea of parseTradingCentralText(text, ctx)) {
          if (seenHash.has(idea.hash)) continue;
          seenHash.add(idea.hash);
          ideas.push(idea);
        }
      }
      if (ideas.length > 0) return ideas;
    } catch (err) {
      this.logger.warn(
        `frame.evaluate card walk failed: ${err instanceof Error ? err.message : String(err)}`,
      );
    }

    return [];
  }

  private extractEmbeddedJson(html: string, ctx: ExtractContext): TradingIdea[] {
    const patterns = [
      /(?:__TC_IDEAS__|tradingCentralIdeas|tcIdeas|recogniaIdeas|initialState)\s*=\s*(\{[\s\S]*?\}|\[[\s\S]*?\])\s*;/,
      /<script[^>]*type=["']application\/json["'][^>]*>([\s\S]*?)<\/script>/i,
    ];
    for (const re of patterns) {
      const m = html.match(re);
      if (!m) continue;
      try {
        const parsed = JSON.parse(m[1]) as unknown;
        const ideas = this.fromNetworkPayloads([parsed], ctx);
        if (ideas.length > 0) return ideas;
      } catch {
        // ignore
      }
    }
    return [];
  }

  /**
   * Best-effort parse of Trading Central-style idea cards from HTML.
   * Looks for data attributes and common card class patterns.
   */
  parseCardsFromHtml(html: string, ctx: ExtractContext): TradingIdea[] {
    const ideas: TradingIdea[] = [];

    const attrCardRe =
      /<(?:div|article|section|li)[^>]*\b(?:data-instrument|data-symbol|data-product)=["']([^"']+)["'][^>]*>/gi;
    let m: RegExpExecArray | null;
    const attrStarts: { index: number; instrument: string }[] = [];
    while ((m = attrCardRe.exec(html)) !== null) {
      attrStarts.push({ index: m.index, instrument: m[1] });
    }

    if (attrStarts.length > 0) {
      for (let i = 0; i < attrStarts.length; i++) {
        const start = attrStarts[i].index;
        const end =
          i + 1 < attrStarts.length ? attrStarts[i + 1].index : start + 4000;
        const chunk = html.slice(start, end);
        const idea = this.parseCardChunk(chunk, attrStarts[i].instrument, ctx);
        if (idea) ideas.push(idea);
      }
      return ideas;
    }

    const classCardRe =
      /<(?:div|article|section)[^>]*class=["'][^"']*(?:idea-card|tc-card|trading-idea|signal-card|story-card|product-card|analysis-card)[^"']*["'][^>]*>[\s\S]*?<\/(?:div|article|section)>/gi;
    const classMatches = html.match(classCardRe) ?? [];
    for (const chunk of classMatches) {
      const idea = this.parseCardChunk(chunk, '', ctx);
      if (idea) ideas.push(idea);
    }

    const embedded = this.extractEmbeddedJson(html, ctx);
    if (embedded.length > 0) return embedded;

    // iframe host markup itself is useless (just the shell) — try plain text from HTML strip
    if (ideas.length === 0) {
      const text = html
        .replace(/<script[\s\S]*?<\/script>/gi, ' ')
        .replace(/<style[\s\S]*?<\/style>/gi, ' ')
        .replace(/<[^>]+>/g, ' ')
        .replace(/\s+/g, ' ')
        .trim();
      return this.parseIdeasFromPlainText(text, ctx);
    }

    return ideas;
  }

  /**
   * Parse free text blocks that look like TC cards (live Recognia layout).
   */
  parseIdeasFromPlainText(text: string, ctx: ExtractContext): TradingIdea[] {
    return parseTradingCentralText(text, ctx);
  }

  private parseCardChunk(
    chunk: string,
    instrumentHint: string,
    ctx: ExtractContext,
  ): TradingIdea | null {
    const attr = (name: string): string | undefined => {
      const re = new RegExp(`data-${name}=["']([^"']+)["']`, 'i');
      const m = chunk.match(re);
      return m?.[1];
    };
    const textOf = (cls: string): string | undefined => {
      const re = new RegExp(
        `class=["'][^"']*${cls}[^"']*["'][^>]*>([^<]+)`,
        'i',
      );
      const m = chunk.match(re);
      return m?.[1]?.trim();
    };

    const instrument =
      instrumentHint ||
      attr('instrument') ||
      attr('symbol') ||
      attr('product') ||
      textOf('instrument') ||
      textOf('symbol') ||
      '';
    if (!instrument) return null;

    const timeframe =
      attr('timeframe') ||
      attr('term') ||
      textOf('timeframe') ||
      textOf('term') ||
      'UNKNOWN';

    const directionRaw =
      attr('direction') ||
      attr('bias') ||
      textOf('direction') ||
      textOf('bias') ||
      'NEUTRAL';

    const target = parseNumber(
      attr('target') || textOf('target') || textOf('target-price'),
    );
    const pivot = parseNumber(attr('pivot') || textOf('pivot'));
    const ideaTimestamp = toIsoTimestamp(
      attr('timestamp') || attr('time') || textOf('timestamp') || textOf('time'),
      ctx.capturedAt,
    );

    return normalizeIdea(
      {
        instrument,
        timeframe,
        direction: directionRaw,
        target,
        pivot,
        ideaTimestamp,
        raw: { htmlSnippet: chunk.slice(0, 500) },
      },
      {
        provider: this.provider,
        sourceUrl: ctx.sourceUrl,
        capturedAt: ctx.capturedAt,
        screenshotPath: ctx.screenshotPath,
      },
    );
  }
}
