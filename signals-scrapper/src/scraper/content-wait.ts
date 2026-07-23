import { Logger } from '@nestjs/common';
import { Frame, Page } from 'playwright';
import { ProviderType } from '../models/trading-idea.model';

const logger = new Logger('ContentWait');

/** Selectors / URL fragments that identify the research widget iframe per provider. */
export const IFRAME_SELECTORS: Record<ProviderType, string[]> = {
  TRADING_CENTRAL: [
    'iframe#tradingcentral',
    'iframe[id="tradingcentral"]',
    'iframe[src*="recognia.com"]',
    'iframe[src*="tradingcentral"]',
    'iframe[src*="tkn="]',
  ],
  AUTOCHARTIST: [
    'iframe#autochartist',
    'iframe[src*="autochartist"]',
    'iframe[src*="AutoChartist"]',
  ],
};

/**
 * Match the *widget* iframe document, not the IC Markets host shell
 * (secure.icmarkets.com/.../TradingCentral is the parent page, not the ideas frame).
 */
export const IFRAME_URL_PATTERNS: Record<ProviderType, RegExp[]> = {
  TRADING_CENTRAL: [/recognia\.com/i, /serve\.shtml/i],
  AUTOCHARTIST: [/autochartist\./i, /autochartist\.com/i],
};

export interface WaitForResearchContentOptions {
  provider: ProviderType;
  /** Hard wait after navigation / iframe attach before reading content. */
  contentWaitMs: number;
  /** Max time to wait for the iframe element itself. */
  iframeTimeoutMs?: number;
  /** Injectable sleep for tests. */
  sleep?: (ms: number) => Promise<void>;
}

function defaultSleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/**
 * After the host page has navigated:
 * 1. Wait for the provider research iframe (e.g. #tradingcentral → recognia.com)
 * 2. Wait contentWaitMs so the iframe can load trade ideas (default 10s)
 * 3. Return the best Frame to scrape, or null if none found
 */
export async function waitForResearchContent(
  page: Page,
  opts: WaitForResearchContentOptions,
): Promise<Frame | null> {
  const sleep = opts.sleep ?? defaultSleep;
  const iframeTimeout = opts.iframeTimeoutMs ?? Math.min(opts.contentWaitMs, 15000);

  let frame: Frame | null = null;

  try {
    frame = await waitForProviderIframe(page, opts.provider, iframeTimeout);
    if (frame) {
      logger.log(
        `Research iframe ready for ${opts.provider}: ${frame.url() || '(about:blank)'}`,
      );
    } else {
      logger.warn(
        `No research iframe detected for ${opts.provider} within ${iframeTimeout}ms; will still wait and try all frames`,
      );
    }
  } catch (err) {
    logger.warn(
      `Iframe wait failed for ${opts.provider}: ${err instanceof Error ? err.message : String(err)}`,
    );
  }

  if (opts.contentWaitMs > 0) {
    logger.log(
      `Waiting ${opts.contentWaitMs}ms for ${opts.provider} widget content to load`,
    );
    await sleep(opts.contentWaitMs);
  }

  // Prefer the iframe frame after settle (URL may only be set once loaded)
  if (!frame || frame.isDetached?.()) {
    frame = findProviderFrame(page, opts.provider);
  } else {
    // Re-resolve in case navigation replaced the frame
    const refreshed = findProviderFrame(page, opts.provider);
    if (refreshed) frame = refreshed;
  }

  if (frame) {
    logger.log(
      `Using frame for extraction: ${frame.url() || frame.name() || 'unnamed'}`,
    );
  }

  return frame;
}

/**
 * Wait until a matching iframe element is in the DOM (and optionally has a src).
 */
export async function waitForProviderIframe(
  page: Page,
  provider: ProviderType,
  timeoutMs: number,
): Promise<Frame | null> {
  const selectors = IFRAME_SELECTORS[provider] ?? [];
  const deadline = Date.now() + timeoutMs;

  for (const selector of selectors) {
    const remaining = deadline - Date.now();
    if (remaining <= 0) break;
    try {
      await page.waitForSelector(selector, {
        state: 'attached',
        timeout: remaining,
      });
      // Give the browser a moment to attach the Frame object
      await page.waitForTimeout(300).catch(() => undefined);
      const frame = findProviderFrame(page, provider);
      if (frame) return frame;

      // Element exists but frame not mapped yet — try contentFrame via handle
      const handle = await page.$(selector);
      if (handle) {
        const contentFrame = await handle.contentFrame();
        if (contentFrame) return contentFrame;
      }
    } catch {
      // try next selector
    }
  }

  return findProviderFrame(page, provider);
}

/**
 * Find the best matching child frame for a provider (by URL / name / parent iframe id).
 */
export function findProviderFrame(
  page: Page,
  provider: ProviderType,
): Frame | null {
  const urlPatterns = IFRAME_URL_PATTERNS[provider] ?? [];
  const frames = page.frames();
  const main = page.mainFrame();

  // 1. URL match on child frames only (never the IC Markets shell)
  for (const frame of frames) {
    if (frame === main) continue;
    const url = frame.url();
    if (urlPatterns.some((re) => re.test(url))) {
      return frame;
    }
  }

  // 2. Frame name (iframe name="tradingcentral")
  const nameHints =
    provider === 'TRADING_CENTRAL'
      ? ['tradingcentral', 'recognia', 'tc']
      : ['autochartist', 'ac'];
  for (const frame of frames) {
    if (frame === main) continue;
    const name = (frame.name() || '').toLowerCase();
    if (nameHints.some((h) => name.includes(h))) {
      return frame;
    }
  }

  // 3. Any non-main, non-blank child frame (last resort)
  for (const frame of frames) {
    if (frame === main) continue;
    const url = frame.url();
    if (url && url !== 'about:blank' && !url.startsWith('chrome')) {
      // Skip same-origin host paths that are clearly the shell, not the widget
      if (/secure\.icmarkets\.com/i.test(url) && /TradingCentral|AutoChartist/i.test(url)) {
        continue;
      }
      return frame;
    }
  }

  return null;
}

/**
 * Pure helper for tests: pick best frame descriptor from URL list.
 */
export function pickFrameUrl(
  frameUrls: string[],
  provider: ProviderType,
): string | null {
  const patterns = IFRAME_URL_PATTERNS[provider] ?? [];
  // Prefer recognia / widget hosts; never pick the IC Markets host shell.
  for (const url of frameUrls) {
    if (/secure\.icmarkets\.com/i.test(url)) continue;
    if (patterns.some((re) => re.test(url))) return url;
  }
  for (const url of frameUrls) {
    if (patterns.some((re) => re.test(url))) return url;
  }
  return null;
}

/**
 * True when Recognia iframe text looks like loaded idea cards (not an empty shell).
 */
export function isTradingCentralContentReady(text: string): boolean {
  const lower = text.toLowerCase();
  const hasPips = lower.includes('pips');
  const hasTrade = /\btrade\b/.test(lower);
  const hasTarget = lower.includes('target');
  const hasPivot = lower.includes('pivot');
  return (hasPips && hasTrade) || (hasTarget && hasPivot);
}

export interface WaitForTradingCentralCardsOptions {
  timeoutMs: number;
  pollIntervalMs?: number;
  sleep?: (ms: number) => Promise<void>;
}

/**
 * Poll the Trading Central Recognia iframe until idea-card markers appear.
 * Returns true when ready, false when the timeout elapses.
 */
export async function waitForTradingCentralCards(
  page: Page,
  opts: WaitForTradingCentralCardsOptions,
): Promise<boolean> {
  const sleep = opts.sleep ?? defaultSleep;
  const pollIntervalMs = opts.pollIntervalMs ?? 500;
  const deadline = Date.now() + opts.timeoutMs;

  while (Date.now() < deadline) {
    const frame = findProviderFrame(page, 'TRADING_CENTRAL');
    if (frame) {
      try {
        const text = await frame.evaluate(
          () => document.body?.innerText ?? '',
        );
        if (isTradingCentralContentReady(text)) {
          logger.log('Trading Central idea cards are visible in the Recognia iframe');
          return true;
        }
      } catch {
        // Frame may still be loading.
      }
    }
    const remaining = deadline - Date.now();
    if (remaining <= 0) break;
    await sleep(Math.min(pollIntervalMs, remaining));
  }

  logger.warn(
    `Trading Central idea cards did not become visible within ${opts.timeoutMs}ms`,
  );
  return false;
}
