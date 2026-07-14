import { Page } from 'playwright';
import { ProviderType, TradingIdea } from '../models/trading-idea.model';
import { NetworkCaptureSession } from './network-capture';

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
  extract(page: Page | null, ctx: ExtractContext): Promise<TradingIdea[]>;
}

export const IDEA_EXTRACTORS = Symbol('IDEA_EXTRACTORS');
