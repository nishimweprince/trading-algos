import { describe, it, expect, vi, afterEach } from 'vitest';
import { RpcClient, RpcError } from '../src/core/rpc.ts';

const URL = 'https://mainnet.test/?api-key=secret123';

function okJson(result: unknown) {
  return { ok: true, status: 200, json: async () => ({ jsonrpc: '2.0', id: 1, result }) };
}
function httpStatus(status: number) {
  return { ok: false, status, json: async () => ({}) };
}
function rpcErrorBody(code: number, message: string) {
  return { ok: true, status: 200, json: async () => ({ jsonrpc: '2.0', id: 1, error: { code, message } }) };
}

describe('RpcClient account/status methods', () => {
  afterEach(() => vi.unstubAllGlobals());

  it('getBalance returns lamports as bigint', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => okJson({ value: 269417611 })));
    const rpc = new RpcClient({ httpUrl: URL });
    expect(await rpc.getBalance('addr')).toBe(269417611n);
  });

  it('getSignatureStatuses maps confirmation status + err', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => okJson({ value: [{ slot: 5, confirmationStatus: 'confirmed', err: null }, null] })));
    const rpc = new RpcClient({ httpUrl: URL });
    const r = await rpc.getSignatureStatuses(['sigA', 'sigB']);
    expect(r[0]).toMatchObject({ slot: 5, confirmationStatus: 'confirmed', err: null });
    expect(r[1]).toBeNull();
  });

  it('getTokenAccountBalance returns amount+decimals, null when account absent', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => okJson({ value: { amount: '1000000', decimals: 6 } })));
    let rpc = new RpcClient({ httpUrl: URL });
    expect(await rpc.getTokenAccountBalance('ata')).toEqual({ amount: 1000000n, decimals: 6 });

    vi.stubGlobal('fetch', vi.fn(async () => rpcErrorBody(-32602, 'Invalid param: could not find account')));
    rpc = new RpcClient({ httpUrl: URL });
    expect(await rpc.getTokenAccountBalance('missing')).toBeNull();
  });
});

describe('RpcClient retries', () => {
  afterEach(() => vi.unstubAllGlobals());

  it('retries transient 429 then succeeds', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(httpStatus(429))
      .mockResolvedValueOnce(httpStatus(503))
      .mockResolvedValueOnce(okJson(42));
    vi.stubGlobal('fetch', fetchMock);

    const rpc = new RpcClient({ httpUrl: URL });
    await expect(rpc.getSlot('confirmed')).resolves.toBe(42);
    expect(fetchMock).toHaveBeenCalledTimes(3);
  });

  it('does not retry a non-transient JSON-RPC error', async () => {
    const fetchMock = vi.fn().mockResolvedValue(rpcErrorBody(-32602, 'Invalid params'));
    vi.stubGlobal('fetch', fetchMock);

    const rpc = new RpcClient({ httpUrl: URL });
    await expect(rpc.getSlot()).rejects.toBeInstanceOf(RpcError);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it('gives up after exhausting retries', async () => {
    const fetchMock = vi.fn().mockResolvedValue(httpStatus(500));
    vi.stubGlobal('fetch', fetchMock);

    const rpc = new RpcClient({ httpUrl: URL, retries: 1 });
    await expect(rpc.getSlot()).rejects.toBeInstanceOf(RpcError);
    expect(fetchMock).toHaveBeenCalledTimes(2); // initial + 1 retry
  });

  it('bounds concurrent in-flight requests to maxConcurrent', async () => {
    let inFlight = 0;
    let peak = 0;
    const fetchMock = vi.fn().mockImplementation(async () => {
      inFlight++;
      peak = Math.max(peak, inFlight);
      await new Promise((r) => setTimeout(r, 20));
      inFlight--;
      return okJson(1);
    });
    vi.stubGlobal('fetch', fetchMock);

    const rpc = new RpcClient({ httpUrl: URL, maxConcurrent: 3 });
    await Promise.all(Array.from({ length: 12 }, () => rpc.getSlot()));
    expect(fetchMock).toHaveBeenCalledTimes(12);
    expect(peak).toBeLessThanOrEqual(3);
  });
});

/**
 * A single exhausted endpoint is not a visible outage — every enrichment field
 * degrades to `unknown`, and in live mode unknowns are vetoes. So a dead RPC
 * silently becomes a 100% veto rate. Failover is what keeps screening alive.
 */
describe('RpcClient endpoint failover', () => {
  afterEach(() => vi.unstubAllGlobals());

  const PRIMARY = 'https://primary.test/?api-key=p';
  const FALLBACK = 'https://fallback.test/?api-key=f';

  it('fails over to a fallback when the primary is rate-limited', async () => {
    const fetchMock = vi.fn(async (url: string) =>
      url.startsWith('https://primary.test') ? httpStatus(429) : okJson(42),
    );
    vi.stubGlobal('fetch', fetchMock);

    const rpc = new RpcClient({ httpUrl: PRIMARY, fallbackHttpUrls: [FALLBACK] });
    expect(await rpc.getSlot()).toBe(42);
    expect(fetchMock.mock.calls.map((c) => String(c[0]).split('/?')[0])).toEqual([
      'https://primary.test',
      'https://fallback.test',
    ]);
  });

  it('parks a rate-limited endpoint so later calls skip it entirely', async () => {
    const fetchMock = vi.fn(async (url: string) =>
      url.startsWith('https://primary.test') ? httpStatus(429) : okJson(1),
    );
    vi.stubGlobal('fetch', fetchMock);

    let clock = 1_000;
    const rpc = new RpcClient({
      httpUrl: PRIMARY,
      fallbackHttpUrls: [FALLBACK],
      now: () => clock,
    });

    await rpc.getSlot();
    fetchMock.mockClear();

    // Still inside the cooldown: the dead primary must not be probed again.
    clock += 5_000;
    await rpc.getSlot();
    expect(fetchMock.mock.calls.every((c) => String(c[0]).startsWith('https://fallback.test'))).toBe(true);
  });

  it('returns to the primary once its cooldown expires', async () => {
    let primaryHealthy = false;
    const fetchMock = vi.fn(async (url: string) => {
      if (url.startsWith('https://primary.test')) return primaryHealthy ? okJson(7) : httpStatus(429);
      return okJson(1);
    });
    vi.stubGlobal('fetch', fetchMock);

    let clock = 1_000;
    const rpc = new RpcClient({ httpUrl: PRIMARY, fallbackHttpUrls: [FALLBACK], now: () => clock });
    await rpc.getSlot();

    primaryHealthy = true;
    clock += 31_000; // past ENDPOINT_COOLDOWN_MS
    fetchMock.mockClear();
    expect(await rpc.getSlot()).toBe(7);
    expect(String(fetchMock.mock.calls[0]![0])).toContain('primary.test');
  });

  it('surfaces the error when every endpoint is exhausted', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => httpStatus(429)));
    const rpc = new RpcClient({ httpUrl: PRIMARY, fallbackHttpUrls: [FALLBACK], retries: 0 });
    await expect(rpc.getSlot()).rejects.toBeInstanceOf(RpcError);
    expect(rpc.allEndpointsCoolingDown).toBe(true);
  });

  it('does not fail over on a non-transient error', async () => {
    const fetchMock = vi.fn(async () => rpcErrorBody(-32602, 'Invalid params'));
    vi.stubGlobal('fetch', fetchMock);
    const rpc = new RpcClient({ httpUrl: PRIMARY, fallbackHttpUrls: [FALLBACK] });
    await expect(rpc.getSlot()).rejects.toBeInstanceOf(RpcError);
    // A bad request is bad everywhere — retrying it elsewhere just wastes quota.
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});
