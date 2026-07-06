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
});
