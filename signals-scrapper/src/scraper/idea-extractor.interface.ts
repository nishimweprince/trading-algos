import { Page } from 'playwright';
import { ProviderType, TradingIdea } from '../models/trading-idea.model';
import { NetworkCaptureSession } from './network-capture';
import type { OpenAiExtractionResult } from '../openai/openai-vision.service';

export interface ExtractContext {
  sourceUrl: string;
  provider: ProviderType;
  capturedAt: string;
  screenshotPath?: string;
  /**
   * Pre-captured network payloads from a session started BEFORE navigation.
   * Prefer this path so research API responses during page load are not missed.
   */
  networkPayloads?: unknown[];
}

/**
 * Provider-specific idea extractor. ScraperService:
 *  1. creates a page
 *  2. starts network capture via beginNetworkCapture(page)
 *  3. navigates
 *  4. finishes capture and passes payloads into extract()
 */
export interface IdeaExtractor {
  readonly provider: ProviderType;
  /** URL patterns used for research API interception. */
  readonly networkUrlPatterns: RegExp[];
  /**
   * Attach response listeners BEFORE navigation. Caller must finish() after
   * navigation (and any settle wait) so in-flight json() parses complete.
   */
  beginNetworkCapture?(page: Page): NetworkCaptureSession;
  /**
   * Optional pre-screenshot interaction (e.g. Autochartist clicks the
   * "Our Favourites" tab). Called after the page settles and before the
   * screenshot is taken.
   */
  prepareForScreenshot?(page: Page, ctx: ExtractContext): Promise<void>;
  /**
   * Vision extraction with OpenAI diagnostics (model + raw response), used by
   * the screenshot→OpenAI live path so ScraperService can persist debug runs.
   */
  extractWithDebug?(ctx: ExtractContext): Promise<OpenAiExtractionResult>;
  extract(page: Page | null, ctx: ExtractContext): Promise<TradingIdea[]>;
}

export const IDEA_EXTRACTORS = Symbol('IDEA_EXTRACTORS');
