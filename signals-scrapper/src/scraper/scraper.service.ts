import { Injectable, Logger, Optional } from '@nestjs/common';
import { Page } from 'playwright';
import {
  BrowserAccessError,
  BrowserService,
} from '../browser/browser.service';
import { AppConfigService } from '../config/app-config.service';
import { SourceConfig } from '../config/sources.schema';
import { DedupService } from '../dedup/dedup.service';
import { DebugRunRecord, DebugRunStage } from '../dedup/seen-store';
import { JsonlLoggerService } from '../logging/jsonl-logger.service';
import { ProviderType, TradingIdea } from '../models/trading-idea.model';
import { Mt5ExecutionService } from '../mt5/mt5-execution.service';
import { Mt5DeliverySummary } from '../mt5/mt5.types';
import { AutochartistExtractor } from './extractors/autochartist.extractor';
import {
  TradingCentralExtractionError,
  TradingCentralExtractionResult,
  TradingCentralExtractor,
} from './extractors/trading-central.extractor';
import { waitForResearchContent } from './content-wait';
import { IdeaExtractor } from './idea-extractor.interface';
import { isLoginWall } from './login-wall';
import { NetworkCaptureSession } from './network-capture';

export interface SourceRunResult {
  source: SourceConfig;
  ok: boolean;
  skippedReason?: string;
  extracted: number;
  newIdeas: number;
  screenshotPath?: string;
  ideas: TradingIdea[];
}

export interface RunAllResult {
  startedAt: string;
  finishedAt: string;
  results: SourceRunResult[];
  totalNew: number;
  mt5?: Mt5DeliverySummary;
}

export interface AuthRefreshResult {
  source: SourceConfig;
  ok: boolean;
  error?: string;
}

export interface RefreshAllResult {
  startedAt: string;
  finishedAt: string;
  results: AuthRefreshResult[];
  refreshed: number;
}

/**
 * Optional hooks for tests to inject pages / skip real browser I/O.
 *
 * Live path order (must not change — network capture depends on it):
 *   getPage → beginNetworkCapture → navigate → wait iframe + CONTENT_WAIT_MS
 *   → login/screenshot → finish capture → extract
 * The same browser window/page is reused across schedule ticks (not closed each run).
 */
export interface ScrapeHooks {
  /**
   * Full control of open flow. If set, this replaces getPage+navigate.
   * Prefer createPage + navigate for tests that assert capture-before-nav order.
   */
  openPage?: (source: SourceConfig) => Promise<Page | null>;
  /** Obtain a page (no navigation). Live path reuses one shared page. */
  createPage?: (source: SourceConfig) => Promise<Page | null>;
  /** Navigate an existing page. Called only when createPage was used. */
  navigate?: (page: Page, source: SourceConfig) => Promise<void>;
  /**
   * Called after createPage and before navigate so tests can assert ordering.
   * Receives the capture session that was just started.
   */
  onCaptureStarted?: (
    source: SourceConfig,
    session: NetworkCaptureSession,
    page: Page,
  ) => void;
  /**
   * Override post-navigation content wait (iframe + settle).
   * Tests should no-op this to stay fast.
   */
  waitForContent?: (page: Page | null, source: SourceConfig) => Promise<void>;
  /** Provide pre-captured network payloads (skips session.finish). */
  networkPayloads?: (source: SourceConfig) => unknown[] | undefined;
  /** Force login-wall detection result. */
  isLoginWall?: (page: Page | null, source: SourceConfig) => Promise<boolean>;
  /** Skip real screenshot; return a fake path. */
  takeScreenshot?: (
    page: Page | null,
    sourceType: string,
  ) => Promise<string | undefined>;
  /**
   * End-of-run page release. Live path uses BrowserService.releasePage (keeps window open).
   * Tests may still hard-close.
   */
  closePage?: (page: Page | null) => Promise<void>;
}

@Injectable()
export class ScraperService {
  private readonly logger = new Logger(ScraperService.name);
  private readonly extractors: Map<ProviderType, IdeaExtractor>;
  private hooks: ScrapeHooks = {};

  constructor(
    private readonly config: AppConfigService,
    private readonly browser: BrowserService,
    private readonly dedup: DedupService,
    private readonly jsonl: JsonlLoggerService,
    private readonly tradingCentral: TradingCentralExtractor,
    private readonly autochartist: AutochartistExtractor,
    @Optional() private readonly mt5?: Mt5ExecutionService,
  ) {
    this.extractors = new Map<ProviderType, IdeaExtractor>([
      ['TRADING_CENTRAL', this.tradingCentral],
      ['AUTOCHARTIST', this.autochartist],
    ]);
  }

  /** Test-only: inject hooks for fixture-backed runs without Chrome. */
  setHooks(hooks: ScrapeHooks): void {
    this.hooks = hooks;
  }

  clearHooks(): void {
    this.hooks = {};
  }

  getExtractor(type: ProviderType): IdeaExtractor | undefined {
    return this.extractors.get(type);
  }

  /**
   * Reload authenticated source tabs without taking screenshots or extracting
   * signals. This keeps browser sessions active independently of signal runs.
   */
  async refreshAuthenticatedPages(): Promise<RefreshAllResult> {
    const startedAt = new Date().toISOString();
    const results: AuthRefreshResult[] = [];

    for (const source of this.config.sources) {
      let page: Page | null = null;
      try {
        page = await this.browser.getPage(source.url);
        await this.browser.reload(page);
        results.push({ source, ok: true });
        this.logger.log(`Authentication refresh complete for ${source.type}`);
      } catch (err) {
        const error = err instanceof Error ? err.message : String(err);
        results.push({ source, ok: false, error });
        this.logger.warn(
          `Authentication refresh failed for ${source.type}: ${error}`,
        );
      } finally {
        if (page) {
          await this.browser.releasePage(page);
        }
      }
    }

    return {
      startedAt,
      finishedAt: new Date().toISOString(),
      results,
      refreshed: results.filter((result) => result.ok).length,
    };
  }

  async runAllSources(): Promise<RunAllResult> {
    const startedAt = new Date().toISOString();
    const results: SourceRunResult[] = [];
    let totalNew = 0;

    for (const source of this.config.sources) {
      const result = await this.runSource(source);
      results.push(result);
      totalNew += result.newIdeas;
    }

    let mt5: Mt5DeliverySummary | undefined;
    if (this.mt5) {
      try {
        mt5 = await this.mt5.processOutbox();
      } catch (err) {
        this.logger.warn(
          `MT5 outbox processing failed: ${err instanceof Error ? err.message : String(err)}`,
        );
      }
    }

    const finishedAt = new Date().toISOString();
    this.logger.log(
      `Run complete: ${results.length} source(s), ${totalNew} new idea(s)`,
    );
    return { startedAt, finishedAt, results, totalNew, mt5 };
  }

  async runSource(source: SourceConfig): Promise<SourceRunResult> {
    const extractor = this.extractors.get(source.type);
    if (!extractor) {
      this.logger.warn(`No extractor for type ${source.type}; skipping`);
      return {
        source,
        ok: false,
        skippedReason: `unknown provider ${source.type}`,
        extracted: 0,
        newIdeas: 0,
        ideas: [],
      };
    }

    let page: Page | null = null;
    let screenshotPath: string | undefined;
    let capture: NetworkCaptureSession | null = null;
    const capturedAt = new Date().toISOString();
    let stage: DebugRunStage = 'browser';
    let tcDiagnostics: TradingCentralExtractionResult | undefined;
    // Tests that explicitly inject legacy network payloads keep exercising the
    // old pure parser. The real configured source always uses OpenAI vision.
    const useTradingCentralVision =
      source.type === 'TRADING_CENTRAL' && !this.hooks.networkPayloads;

    try {
      // ---- 1. Obtain page + start network capture BEFORE navigation ----
      if (this.hooks.openPage) {
        // Legacy full-open hook: capture cannot see nav responses; tests that
        // use this must inject networkPayloads themselves.
        page = await this.hooks.openPage(source);
        if (page && extractor.beginNetworkCapture && !useTradingCentralVision) {
          capture = extractor.beginNetworkCapture(page);
          this.hooks.onCaptureStarted?.(source, capture, page);
        }
      } else {
        // Reuse one shared page across cron ticks (getPage, not a fresh window).
        page = this.hooks.createPage
          ? await this.hooks.createPage(source)
          : useTradingCentralVision
            ? await this.browser.getPage(source.url)
            : await this.browser.getPage();

        if (page) {
          if (extractor.beginNetworkCapture && !useTradingCentralVision) {
            // CRITICAL for network extractors: attach before navigation.
            capture = extractor.beginNetworkCapture(page);
            this.hooks.onCaptureStarted?.(source, capture, page);
          }

          stage = 'navigation';
          if (this.hooks.navigate) {
            await this.hooks.navigate(page, source);
          } else if (useTradingCentralVision) {
            await this.browser.reload(page);
          } else {
            await this.browser.navigate(page, source.url);
          }
        }
      }

      // ---- 2. Wait for research iframe + settle (TC loads ideas in Recognia iframe) ----
      // Do NOT check login wall / content immediately after navigate.
      if (this.hooks.waitForContent) {
        await this.hooks.waitForContent(page, source);
      } else if (page) {
        await waitForResearchContent(page, {
          provider: source.type,
          contentWaitMs: this.config.contentWaitMs,
          iframeTimeoutMs: this.config.navTimeoutMs,
        });
      }

      // ---- 3. Login wall check (after settle so shell can finish rendering) ----
      stage = 'login';
      const loginWall = this.hooks.isLoginWall
        ? await this.hooks.isLoginWall(page, source)
        : page
          ? await isLoginWall(page)
          : false;

      // ---- 4. Screenshot for audit (even on skip paths when we have a page) ----
      stage = 'screenshot';
      if (page || this.hooks.takeScreenshot) {
        try {
          screenshotPath = this.hooks.takeScreenshot
            ? await this.hooks.takeScreenshot(page, source.type)
            : page
              ? await this.browser.takeScreenshot(page, source.type)
              : undefined;
        } catch (err) {
          if (useTradingCentralVision) throw err;
          this.logger.warn(
            `Screenshot failed for ${source.type}: ${err instanceof Error ? err.message : String(err)}`,
          );
        }
      }

      if (loginWall) {
        this.logger.warn(
          `Login wall detected for ${source.type} (${source.url}); skipping extraction`,
        );
        capture?.dispose();
        if (useTradingCentralVision) {
          this.safeRecordRun({
            id: `${capturedAt}:${source.type}`,
            provider: source.type,
            sourceUrl: source.url,
            capturedAt,
            status: 'failed',
            stage: 'login',
            screenshotPath,
            error: 'login_wall',
          });
        }
        return {
          source,
          ok: false,
          skippedReason: 'login_wall',
          extracted: 0,
          newIdeas: 0,
          screenshotPath,
          ideas: [],
        };
      }

      // ---- 5. Finish capture (awaits in-flight response.json() from iframe APIs) ----
      let networkPayloads = this.hooks.networkPayloads?.(source);
      if (networkPayloads === undefined && capture) {
        // Short extra settle; the 10s content wait already covered iframe load.
        networkPayloads = await capture.finish({ settleMs: 500 });
        capture = null; // finished (listener already detached)
      } else if (capture) {
        capture.dispose();
        capture = null;
      }

      // ---- 6. Extract (network primary via payloads, iframe DOM fallback) ----
      let ideas: TradingIdea[];
      if (useTradingCentralVision) {
        stage = 'openai';
        tcDiagnostics = await this.tradingCentral.extractWithDebug({
          sourceUrl: source.url,
          provider: source.type,
          capturedAt,
          screenshotPath,
        });
        ideas = tcDiagnostics.ideas;
        stage = 'validation';
      } else {
        ideas = await extractor.extract(page, {
          sourceUrl: source.url,
          provider: source.type,
          capturedAt,
          screenshotPath,
          networkPayloads,
        });
      }

      const stamped = ideas.map((idea) =>
        idea.screenshotPath || !screenshotPath
          ? idea
          : { ...idea, screenshotPath },
      );

      if (stamped.length === 0) {
        this.logger.warn(
          `No ideas extracted for ${source.type}; empty/partial widget?`,
        );
      }

      const newIdeas = this.dedup.filterNew(stamped);
      const executions =
        this.mt5?.createExecutionRecords(newIdeas) ?? [];
      stage = 'persistence';
      if (newIdeas.length > 0) {
        this.jsonl.appendIdeas(newIdeas);
      }
      if (useTradingCentralVision && tcDiagnostics) {
        const run: DebugRunRecord = {
          id: `${capturedAt}:${source.type}`,
          provider: source.type,
          sourceUrl: source.url,
          capturedAt,
          status: 'success',
          stage: 'complete',
          screenshotPath,
          openai: {
            model: tcDiagnostics.model,
            rawResponse: tcDiagnostics.rawResponse,
          },
          signals: stamped,
          rejected: tcDiagnostics.rejected,
        };
        this.dedup.persistSuccess(newIdeas, run, executions);
      } else if (newIdeas.length > 0) {
        this.dedup.markSeen(newIdeas, executions);
      }

      this.logger.log(
        `${source.type}: extracted=${stamped.length}, new=${newIdeas.length}`,
      );

      return {
        source,
        ok: true,
        extracted: stamped.length,
        newIdeas: newIdeas.length,
        screenshotPath,
        ideas: newIdeas,
      };
    } catch (err) {
      capture?.dispose();
      const msg = err instanceof Error ? err.message : String(err);
      const browserCode =
        err instanceof BrowserAccessError ? err.code : undefined;
      const tcError =
        err instanceof TradingCentralExtractionError ? err : undefined;
      if (useTradingCentralVision) {
        this.safeRecordRun({
          id: `${capturedAt}:${source.type}`,
          provider: source.type,
          sourceUrl: source.url,
          capturedAt,
          status: 'failed',
          stage: tcError?.stage ?? stage,
          screenshotPath,
          openai: tcError?.details.model
            ? {
                model: tcError.details.model,
                rawResponse: tcError.details.rawResponse,
              }
            : undefined,
          error: browserCode ?? msg,
        });
      }
      const isTimeout =
        /timeout|timed out|navigation/i.test(msg) ||
        (err as { name?: string })?.name === 'TimeoutError';
      this.logger.warn(
        `${isTimeout ? 'Timeout' : 'Error'} for ${source.type}: ${msg}`,
      );
      return {
        source,
        ok: false,
        skippedReason: browserCode ?? (isTimeout ? 'timeout' : `error: ${msg}`),
        extracted: 0,
        newIdeas: 0,
        screenshotPath,
        ideas: [],
      };
    } finally {
      if (this.hooks.closePage) {
        await this.hooks.closePage(page);
      } else if (page) {
        // Keep the window open for the next schedule tick.
        await this.browser.releasePage(page);
      }
    }
  }

  private safeRecordRun(run: DebugRunRecord): void {
    try {
      this.dedup.recordRun(run);
    } catch (err) {
      this.logger.warn(
        `Could not persist extraction debug run: ${err instanceof Error ? err.message : String(err)}`,
      );
    }
  }
}
