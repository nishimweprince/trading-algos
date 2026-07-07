import { describe, it, expect, vi, afterEach } from 'vitest';
import { fetchRugcheck } from '../src/enrichment/rugcheck.ts';

function mockFetch(status: number, body: unknown) {
  return vi.fn(async (_url: string, _init?: RequestInit) => ({
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  }));
}

describe('fetchRugcheck', () => {
  afterEach(() => vi.unstubAllGlobals());

  it('inverts RugCheck risk into a goodness score (safe token → high)', async () => {
    vi.stubGlobal('fetch', mockFetch(200, { score_normalised: 9, risks: [{ name: 'Low Liquidity' }] }));
    const r = await fetchRugcheck('mint');
    expect(r?.score).toBe(91); // 100 - 9
    expect(r?.riskNormalised).toBe(9);
    expect(r?.risks).toEqual(['Low Liquidity']);
  });

  it('maps a risky token to a low goodness score', async () => {
    vi.stubGlobal('fetch', mockFetch(200, { score_normalised: 80 }));
    const r = await fetchRugcheck('mint');
    expect(r?.score).toBe(20); // 100 - 80 — a high-risk token is penalized
  });

  it('sends the api key in the apikey header', async () => {
    const f = mockFetch(200, { score_normalised: 5 });
    vi.stubGlobal('fetch', f);
    await fetchRugcheck('mint', { apiKey: 'secret-key' });
    const init = f.mock.calls[0]![1];
    expect(init?.headers).toMatchObject({ apikey: 'secret-key' });
  });

  it('returns null on non-ok / missing score (best-effort, never throws)', async () => {
    vi.stubGlobal('fetch', mockFetch(429, {}));
    expect(await fetchRugcheck('mint')).toBeNull();
    vi.stubGlobal('fetch', mockFetch(200, { risks: [] }));
    expect(await fetchRugcheck('mint')).toBeNull();
  });
});
