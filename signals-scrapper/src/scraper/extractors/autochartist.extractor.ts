import { Injectable, Logger } from '@nestjs/common';
import { Page } from 'playwright';
import { ProviderType, TradingIdea } from '../../models/trading-idea.model';
import {
  ExtractContext,
  IdeaExtractor,
} from '../idea-extractor.interface';
import {
  NetworkCaptureSession,
  startNetworkCapture,
} from '../network-capture';
import {
  normalizeDirection,
  normalizeIdea,
  parseNumber,
  toIsoTimestamp,
} from './normalize';
import { AUTOCHARTIST_NETWORK_PATTERNS } from './network-patterns';

/**
 * Autochartist extractor.
 * Primary: network-response interception of research API JSON
 *   (session must be started BEFORE navigation via beginNetworkCapture).
 * Fallback: DOM / iframe text scraping of pattern/idea cards.
 */
@Injectable()
export class AutochartistExtractor implements IdeaExtractor {
  readonly provider: ProviderType = 'AUTOCHARTIST';
  readonly networkUrlPatterns = AUTOCHARTIST_NETWORK_PATTERNS;
  private readonly logger = new Logger(AutochartistExtractor.name);

  beginNetworkCapture(page: Page): NetworkCaptureSession {
    return startNetworkCapture(page, {
      urlPatterns: this.networkUrlPatterns,
    });
  }

  async extract(page: Page | null, ctx: ExtractContext): Promise<TradingIdea[]> {
    if (ctx.networkPayloads && ctx.networkPayloads.length > 0) {
      const ideas = this.fromNetworkPayloads(ctx.networkPayloads, ctx);
      if (ideas.length > 0) {
        this.logger.log(`Extracted ${ideas.length} idea(s) via network`);
        return ideas;
      }
    }

    if (!page) {
      this.logger.warn('No page and no network payloads; returning empty');
      return [];
    }

    this.logger.log(
      'No pre-nav payloads; starting late capture for residual XHR (prefer beginNetworkCapture before navigate)',
    );
    const session = this.beginNetworkCapture(page);
    try {
      const captured = await session.finish({ settleMs: 1500 });
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

    this.logger.log('Falling back to DOM extraction for Autochartist');
    return this.fromDom(page, ctx);
  }

  fromNetworkPayloads(payloads: unknown[], ctx: ExtractContext): TradingIdea[] {
    const ideas: TradingIdea[] = [];
    for (const payload of payloads) {
      for (const item of this.flattenIdeaItems(payload)) {
        const idea = this.mapApiItem(item, ctx);
        if (idea) ideas.push(idea);
      }
    }
    return ideas;
  }

  extractFromApiJson(payload: unknown, ctx: ExtractContext): TradingIdea[] {
    return this.fromNetworkPayloads([payload], ctx);
  }

  extractFromDomHtml(html: string, ctx: ExtractContext): TradingIdea[] {
    return this.parseCardsFromHtml(html, ctx);
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
      'patterns',
      'results',
      'data',
      'signals',
      'ideas',
      'items',
      'tradeIdeas',
    ]) {
      if (Array.isArray(obj[key])) {
        return this.flattenIdeaItems(obj[key]);
      }
      if (obj[key] && typeof obj[key] === 'object') {
        const nested = obj[key] as Record<string, unknown>;
        for (const k2 of ['patterns', 'results', 'data', 'items']) {
          if (Array.isArray(nested[k2])) {
            return this.flattenIdeaItems(nested[k2]);
          }
        }
      }
    }
    if (obj.instrument || obj.symbol || obj.uid || obj.pattern) {
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
        item.ticker ??
        (item.symbolName as string) ??
        '',
    ).trim();
    if (!instrument) return null;

    const timeframe = String(
      item.timeframe ??
        item.interval ??
        item.period ??
        item.timeInterval ??
        item.granularity ??
        '',
    ).trim() || 'UNKNOWN';

    const direction = normalizeDirection(
      item.direction ??
        item.trend ??
        item.patternDirection ??
        item.bias ??
        item.side,
    );

    const target = parseNumber(
      item.target ??
        item.priceTarget ??
        item.targetPrice ??
        item.predictedPrice ??
        item.tp,
    );
    const pivot = parseNumber(
      item.pivot ?? item.length ?? item.entry ?? item.patternPrice,
    );

    let expectedMovePips: [number, number] | undefined;
    if (Array.isArray(item.expectedMovePips) && item.expectedMovePips.length >= 2) {
      const a = parseNumber(item.expectedMovePips[0]);
      const b = parseNumber(item.expectedMovePips[1]);
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
    } else if (item.support || item.resistance) {
      levels = {
        support: [item.support]
          .flat()
          .map(parseNumber)
          .filter((n): n is number => n !== undefined),
        resistance: [item.resistance]
          .flat()
          .map(parseNumber)
          .filter((n): n is number => n !== undefined),
      };
    }

    const ideaTimestamp = toIsoTimestamp(
      item.ideaTimestamp ??
        item.timestamp ??
        item.time ??
        item.identified ??
        item.patternTime ??
        item.updatedAt,
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
    let html = '';
    try {
      const frames = page.frames();
      for (const frame of frames) {
        const content = await frame.content().catch(() => '');
        if (
          content &&
          (content.includes('pattern') ||
            content.includes('autochartist') ||
            content.includes('data-symbol') ||
            content.includes('ac-card') ||
            content.includes('USD') ||
            content.includes('EUR'))
        ) {
          html = content;
          break;
        }
      }
      if (!html) html = await page.content();
    } catch (err) {
      this.logger.warn(
        `DOM read failed: ${err instanceof Error ? err.message : String(err)}`,
      );
      return [];
    }
    return this.parseCardsFromHtml(html, ctx);
  }

  parseCardsFromHtml(html: string, ctx: ExtractContext): TradingIdea[] {
    const ideas: TradingIdea[] = [];

    const attrCardRe =
      /<(?:div|article|section|li|tr)[^>]*\b(?:data-symbol|data-instrument)=["']([^"']+)["'][^>]*>/gi;
    let m: RegExpExecArray | null;
    const starts: { index: number; instrument: string }[] = [];
    while ((m = attrCardRe.exec(html)) !== null) {
      starts.push({ index: m.index, instrument: m[1] });
    }

    if (starts.length > 0) {
      for (let i = 0; i < starts.length; i++) {
        const start = starts[i].index;
        const end = i + 1 < starts.length ? starts[i + 1].index : start + 4000;
        const chunk = html.slice(start, end);
        const idea = this.parseCardChunk(chunk, starts[i].instrument, ctx);
        if (idea) ideas.push(idea);
      }
      return ideas;
    }

    const classCardRe =
      /<(?:div|article|section)[^>]*class=["'][^"']*(?:pattern-card|ac-card|signal-card|trade-idea)[^"']*["'][^>]*>[\s\S]*?<\/(?:div|article|section)>/gi;
    for (const chunk of html.match(classCardRe) ?? []) {
      const idea = this.parseCardChunk(chunk, '', ctx);
      if (idea) ideas.push(idea);
    }

    const embedded = html.match(
      /(?:__AC_IDEAS__|autochartistPatterns|acPatterns)\s*=\s*(\[[\s\S]*?\])\s*;/,
    );
    if (embedded) {
      try {
        ideas.push(...this.fromNetworkPayloads([JSON.parse(embedded[1])], ctx));
      } catch {
        // ignore
      }
    }

    return ideas;
  }

  private parseCardChunk(
    chunk: string,
    instrumentHint: string,
    ctx: ExtractContext,
  ): TradingIdea | null {
    const attr = (name: string): string | undefined => {
      const re = new RegExp(`data-${name}=["']([^"']+)["']`, 'i');
      return chunk.match(re)?.[1];
    };
    const textOf = (cls: string): string | undefined => {
      const re = new RegExp(
        `class=["'][^"']*${cls}[^"']*["'][^>]*>([^<]+)`,
        'i',
      );
      return chunk.match(re)?.[1]?.trim();
    };

    const instrument =
      instrumentHint ||
      attr('symbol') ||
      attr('instrument') ||
      textOf('symbol') ||
      textOf('instrument') ||
      '';
    if (!instrument) return null;

    const timeframe =
      attr('timeframe') ||
      attr('interval') ||
      textOf('timeframe') ||
      textOf('interval') ||
      'UNKNOWN';
    const directionRaw =
      attr('direction') ||
      attr('trend') ||
      textOf('direction') ||
      textOf('trend') ||
      'NEUTRAL';
    const target = parseNumber(
      attr('target') || textOf('target') || textOf('price-target'),
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
