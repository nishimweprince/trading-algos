import { Injectable, Logger, OnModuleDestroy } from '@nestjs/common';
import { existsSync, mkdirSync } from 'fs';
import { join } from 'path';
import {
  Browser,
  BrowserContext,
  chromium,
  Page,
} from 'playwright';
import { AppConfigService } from '../config/app-config.service';

@Injectable()
export class BrowserService implements OnModuleDestroy {
  private readonly logger = new Logger(BrowserService.name);
  private browser: Browser | null = null;
  private context: BrowserContext | null = null;
  /** Single shared page reused across cron ticks (avoids a new window every run). */
  private sharedPage: Page | null = null;
  /** True when we own the browser (PERSISTENT or launched); false for CDP attach. */
  private ownsBrowser = false;

  constructor(private readonly config: AppConfigService) {}

  async onModuleDestroy(): Promise<void> {
    await this.close();
  }

  /**
   * Ensure a browser context is available (CDP attach or persistent launch).
   * Launches at most once for the process lifetime; subsequent calls reuse it.
   */
  async ensureContext(): Promise<BrowserContext> {
    if (this.context) {
      // Context may have been closed externally — recover if so.
      try {
        // pages() throws if context is closed
        this.context.pages();
        return this.context;
      } catch {
        this.logger.warn('Browser context was closed; relaunching');
        this.context = null;
        this.browser = null;
        this.sharedPage = null;
      }
    }

    const mode = this.config.browserMode;
    if (mode === 'CDP') {
      const endpoint = this.config.cdpEndpoint;
      this.logger.log(`Attaching over CDP: ${endpoint}`);
      this.browser = await chromium.connectOverCDP(endpoint);
      this.ownsBrowser = false;
      const contexts = this.browser.contexts();
      this.context =
        contexts[0] ?? (await this.browser.newContext());
    } else {
      const userDataDir = this.config.userDataDir;
      this.logger.log(`Launching persistent context: ${userDataDir}`);
      this.context = await chromium.launchPersistentContext(userDataDir, {
        headless: this.config.headless,
        viewport: { width: 1440, height: 900 },
      });
      this.ownsBrowser = true;
    }
    return this.context;
  }

  /**
   * Return a page for scraping, reusing the same window/tab across schedule ticks.
   *
   * Prefer an already-open page from the context (persistent launch creates one)
   * before calling context.newPage(), which would open another OS window.
   */
  async getPage(): Promise<Page> {
    const ctx = await this.ensureContext();

    if (this.sharedPage && !this.sharedPage.isClosed()) {
      this.applyTimeouts(this.sharedPage);
      return this.sharedPage;
    }

    const existing = ctx.pages().filter((p) => !p.isClosed());
    if (existing.length > 0) {
      this.sharedPage = existing[0];
      this.logger.log('Reusing existing browser page (no new window)');
      this.applyTimeouts(this.sharedPage);
      return this.sharedPage;
    }

    this.logger.log('Creating initial browser page');
    this.sharedPage = await ctx.newPage();
    this.applyTimeouts(this.sharedPage);
    return this.sharedPage;
  }

  /**
   * @deprecated Prefer getPage() so scheduled runs reuse one window.
   * Still available for callers that explicitly want a fresh tab.
   */
  async newPage(): Promise<Page> {
    return this.getPage();
  }

  /**
   * Navigate an existing page to url. Network capture must already be attached.
   */
  async navigate(page: Page, url: string): Promise<void> {
    this.logger.log(`Navigating to ${url}`);
    await page.goto(url, {
      waitUntil: 'domcontentloaded',
      timeout: this.config.navTimeoutMs,
    });
  }

  /**
   * Convenience: getPage + navigate (reuses the shared window).
   */
  async openPage(url: string): Promise<Page> {
    const page = await this.getPage();
    await this.navigate(page, url);
    return page;
  }

  /**
   * Take a screenshot named by source type and timestamp.
   * Returns the absolute path written.
   */
  async takeScreenshot(
    page: Page,
    sourceType: string,
  ): Promise<string> {
    const dir = this.config.screenshotDir;
    if (!existsSync(dir)) {
      mkdirSync(dir, { recursive: true });
    }
    const ts = new Date().toISOString().replace(/[:.]/g, '-');
    const filename = `${sourceType}_${ts}.png`;
    const path = join(dir, filename);
    await page.screenshot({ path, fullPage: true });
    this.logger.log(`Screenshot saved: ${path}`);
    return path;
  }

  /**
   * End-of-run release: keeps the page open so the next cron tick reuses it.
   * Only clears the shared reference if the page was already closed externally.
   */
  async releasePage(page: Page | null): Promise<void> {
    if (!page) return;
    if (page.isClosed()) {
      if (this.sharedPage === page) {
        this.sharedPage = null;
      }
      return;
    }
    // Intentionally do not page.close() — that forced a new OS window every tick.
  }

  /**
   * Hard-close a page (tests / explicit teardown only). Prefer releasePage in the scrape path.
   */
  async closePage(page: Page): Promise<void> {
    try {
      if (!page.isClosed()) {
        await page.close();
      }
    } catch (err) {
      this.logger.warn(
        `Error closing page: ${err instanceof Error ? err.message : String(err)}`,
      );
    } finally {
      if (this.sharedPage === page) {
        this.sharedPage = null;
      }
    }
  }

  /** Whether the shared browser context is currently held. */
  hasOpenContext(): boolean {
    return this.context !== null;
  }

  /** Whether a live shared page is held (for tests / diagnostics). */
  hasSharedPage(): boolean {
    return !!this.sharedPage && !this.sharedPage.isClosed();
  }

  async close(): Promise<void> {
    try {
      this.sharedPage = null;
      if (this.ownsBrowser && this.context) {
        await this.context.close();
      } else if (this.browser) {
        // Detach from CDP without closing the user's Chrome
        await this.browser.close();
      }
    } catch (err) {
      this.logger.warn(
        `Error closing browser: ${err instanceof Error ? err.message : String(err)}`,
      );
    } finally {
      this.browser = null;
      this.context = null;
      this.sharedPage = null;
    }
  }

  private applyTimeouts(page: Page): void {
    page.setDefaultTimeout(this.config.navTimeoutMs);
    page.setDefaultNavigationTimeout(this.config.navTimeoutMs);
  }
}
