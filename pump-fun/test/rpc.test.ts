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
