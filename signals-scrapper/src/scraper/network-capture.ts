import { Page, Response } from 'playwright';

/**
 * Active response-interception session. Attach BEFORE navigation so XHR/fetch
 * fired during page load are captured. finish() awaits in-flight response.json()
 * work so no payload is dropped on a race.
 */
export interface NetworkCaptureSession {
  /** Await pending json() parses, detach listener, return payloads. */
  finish(opts?: { settleMs?: number }): Promise<unknown[]>;
  /** Detach listener without waiting (error-path cleanup). */
  dispose(): void;
  /** Number of payloads collected so far (for tests / diagnostics). */
  count(): number;
  /** Number of in-flight json() handlers (for tests). */
  pendingCount(): number;
}

export interface StartNetworkCaptureOptions {
  urlPatterns: RegExp[];
  /**
   * Optional content-type filter. Defaults to json / javascript.
   * Pass null to accept any content type that parses as JSON.
   */
  contentTypeFilter?: RegExp | null;
}

/**
 * Subscribe to page 'response' events matching research API URL patterns.
 * MUST be called before page.goto / navigation so navigation-time responses
 * are not missed.
 */
export function startNetworkCapture(
  page: Page,
  opts: StartNetworkCaptureOptions,
): NetworkCaptureSession {
  const captured: unknown[] = [];
  const pending: Promise<void>[] = [];
  let disposed = false;

  const contentTypeFilter =
    opts.contentTypeFilter === null
      ? null
      : (opts.contentTypeFilter ?? /json|javascript/i);

  const onResponse = (response: Response): void => {
    if (disposed) return;
    try {
      const url = response.url();
      if (!opts.urlPatterns.some((re) => re.test(url))) return;
      if (contentTypeFilter) {
        const ct = response.headers()['content-type'] ?? '';
        if (!contentTypeFilter.test(ct)) return;
      }
      // Track the async body read so finish() can await it.
      const work = (async () => {
        try {
          const body = await response.json();
          if (body !== null && body !== undefined) {
            captured.push(body);
          }
        } catch {
          // Non-JSON or closed response — ignore
        }
      })();
      pending.push(work);
    } catch {
      // ignore filter errors
    }
  };

  page.on('response', onResponse);

  return {
    async finish(finishOpts?: { settleMs?: number }): Promise<unknown[]> {
      const settleMs = finishOpts?.settleMs ?? 500;
      if (settleMs > 0 && typeof (page as { waitForTimeout?: (ms: number) => Promise<void> }).waitForTimeout === 'function') {
        await page.waitForTimeout(settleMs).catch(() => undefined);
      }
      // Snapshot current pending; more may still arrive during await, so loop.
      let guard = 0;
      while (pending.length > 0 && guard < 20) {
        const batch = pending.splice(0, pending.length);
        await Promise.all(batch);
        guard += 1;
      }
      disposed = true;
      page.off('response', onResponse);
      return captured.slice();
    },
    dispose(): void {
      disposed = true;
      page.off('response', onResponse);
    },
    count(): number {
      return captured.length;
    },
    pendingCount(): number {
      return pending.length;
    },
  };
}

/**
 * Test double: a minimal EventEmitter-like page that supports on/off('response')
 * and waitForTimeout. Used to unit-test capture ordering without Playwright.
 */
export type ResponseLike = {
  url(): string;
  headers(): Record<string, string>;
  json(): Promise<unknown>;
};

export function createMockPageForCapture(): {
  page: Page;
  emitResponse: (response: ResponseLike) => void;
} {
  const listeners = new Map<string, Set<(...args: unknown[]) => void>>();
  const page = {
    on(event: string, handler: (...args: unknown[]) => void) {
      if (!listeners.has(event)) listeners.set(event, new Set());
      listeners.get(event)!.add(handler);
      return page;
    },
    off(event: string, handler: (...args: unknown[]) => void) {
      listeners.get(event)?.delete(handler);
      return page;
    },
    async waitForTimeout(_ms: number) {
      // no-op in tests (callers control timing)
    },
  } as unknown as Page;

  return {
    page,
    emitResponse(response: ResponseLike) {
      const set = listeners.get('response');
      if (!set) return;
      for (const handler of set) {
        handler(response);
      }
    },
  };
}
