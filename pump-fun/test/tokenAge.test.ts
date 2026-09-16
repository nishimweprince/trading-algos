import { describe, it, expect, vi, afterEach } from 'vitest';
import { fetchTokenAge } from '../src/enrichment/tokenAge.ts';

function mockFetch(status: number, body: unknown) {
  return vi.fn(async (_url: string, _init?: RequestInit) => ({
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  }));
}

describe('fetchTokenAge', () => {
  afterEach(() => vi.unstubAllGlobals());

  it('returns the creation timestamp from pump.fun on a valid response', async () => {
    vi.stubGlobal('fetch', mockFetch(200, { created_timestamp: 1787923888000 }));
    const r = await fetchTokenAge('mint');
    expect(r?.createdAtMs).toBe(1787923888000);
  });

  it('returns null on non-ok / missing timestamp (best-effort, never throws)', async () => {
    vi.stubGlobal('fetch', mockFetch(429, {}));
    expect(await fetchTokenAge('mint')).toBeNull();
    vi.stubGlobal('fetch', mockFetch(200, { name: 'OTC' }));
    expect(await fetchTokenAge('mint')).toBeNull();
  });

  it('returns null when the request throws (e.g. abort/timeout)', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => {
        throw new Error('network error');
      }),
    );
    expect(await fetchTokenAge('mint')).toBeNull();
  });
});
