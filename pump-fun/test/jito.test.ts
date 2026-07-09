import { describe, it, expect, vi, afterEach } from 'vitest';
import { JitoTxSender } from '../src/executor/jito.ts';

describe('JitoTxSender', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('sends base64 transactions to the Jito transaction endpoint with optional auth', async () => {
    const fetch = vi.fn(async (_url: string, init: RequestInit) => {
      const body = JSON.parse(String(init.body)) as { method: string; params: unknown[] };
      expect(body.method).toBe('sendTransaction');
      expect(body.params[0]).toBe(Buffer.from(new Uint8Array([1, 2, 3])).toString('base64'));
      expect((init.headers as Record<string, string>)['x-jito-auth']).toBe('auth-token');
      return new Response(JSON.stringify({ jsonrpc: '2.0', result: 'sig-jito', id: 1 }), {
        headers: { 'x-bundle-id': 'bundle-1' },
      });
    });
    vi.stubGlobal('fetch', fetch);

    const sender = new JitoTxSender({ blockEngineUrl: 'https://ny.mainnet.block-engine.jito.wtf/', authToken: 'auth-token' });
    const res = await sender.send(new Uint8Array([1, 2, 3]));

    expect(fetch).toHaveBeenCalledWith('https://ny.mainnet.block-engine.jito.wtf/api/v1/transactions', expect.any(Object));
    expect(res).toEqual({ signature: 'sig-jito', bundleId: 'bundle-1' });
  });

  it('fetches and caches tip accounts', async () => {
    const fetch = vi.fn(async () =>
      new Response(JSON.stringify({ jsonrpc: '2.0', result: ['tip-a'], id: 1 })),
    );
    vi.stubGlobal('fetch', fetch);

    const sender = new JitoTxSender({ blockEngineUrl: 'https://mainnet.block-engine.jito.wtf' });
    await expect(sender.getTipAccount()).resolves.toBe('tip-a');
    await expect(sender.getTipAccount()).resolves.toBe('tip-a');
    expect(fetch).toHaveBeenCalledOnce();
  });

  it('surfaces Jito HTTP errors', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response('rate limited', { status: 429 })));
    const sender = new JitoTxSender({ blockEngineUrl: 'https://mainnet.block-engine.jito.wtf' });
    await expect(sender.send(new Uint8Array([1]))).rejects.toThrow(/HTTP 429/);
  });
});
