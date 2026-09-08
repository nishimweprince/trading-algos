import { describe, it, expect, vi, afterEach } from 'vitest';
import { HeliusWsFeed, extractAtlasTx } from '../src/detector/heliusWs.ts';
import { WSOL_MINT, PROGRAM_IDS } from '../src/core/constants.ts';
import type { RpcClient } from '../src/core/rpc.ts';
import type { FeedGraduation } from '../src/core/types.ts';

const TOKEN = '64W4CqYgGzco1Rm5cgHepUWPvqPWWTJ6NRRWwLVGpump';

class FakeWs {
  static instance: FakeWs | null = null;
  handlers: Record<string, Array<(ev: unknown) => void>> = {};
  sent: string[] = [];
  url: string;
  constructor(url: string) {
    this.url = url;
    FakeWs.instance = this;
  }
  addEventListener(type: string, cb: (ev: unknown) => void) {
    (this.handlers[type] ??= []).push(cb);
  }
  send(d: string) {
    this.sent.push(d);
  }
  close() {
    this.fire('close', { code: 1000 });
  }
  fire(type: string, ev: unknown) {
    for (const h of this.handlers[type] ?? []) h(ev);
  }
}

function fakeRpc(mints: string[]): RpcClient {
  return { getTransactionTokenMints: async () => mints } as unknown as RpcClient;
}

function logsMsg(logs: string[], signature = 'sig1', err: unknown = null) {
  return JSON.stringify({ jsonrpc: '2.0', method: 'logsNotification', params: { result: { value: { signature, logs, err } } } });
}

describe('HeliusWsFeed', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    FakeWs.instance = null;
  });

  function make(mints: string[]) {
    vi.stubGlobal('WebSocket', FakeWs);
    const grads: FeedGraduation[] = [];
    const health: boolean[] = [];
    const feed = new HeliusWsFeed({
      rpc: fakeRpc(mints),
      httpUrl: 'https://mainnet.helius-rpc.com/?api-key=secret',
      pumpFunProgramId: PROGRAM_IDS.PUMP_FUN,
      reconnectBaseMs: 10,
      reconnectMaxMs: 100,
    });
    feed.onGraduation((g) => grads.push(g));
    feed.onHealth((h) => health.push(h));
    feed.start();
    return { feed, grads, health, ws: FakeWs.instance! };
  }

  it('derives a wss endpoint and subscribes on open', () => {
    const { ws } = make([TOKEN, WSOL_MINT]);
    expect(ws.url).toBe('wss://mainnet.helius-rpc.com/?api-key=secret');
    ws.fire('open', {});
    expect(ws.sent[0]).toContain('logsSubscribe');
    expect(ws.sent[0]).toContain(PROGRAM_IDS.PUMP_FUN);
  });

  it('emits a graduation with the non-WSOL mint on a migrate log', async () => {
    const { grads, health, ws } = make([TOKEN, WSOL_MINT]);
    ws.fire('open', {});
    ws.fire('message', { data: JSON.stringify({ id: 1, result: 42 }) }); // sub ack
    expect(health).toContain(true);
    ws.fire('message', { data: logsMsg(['Program log: Instruction: Migrate'], 'MIGSIG') });
    await vi.waitFor(() => expect(grads).toHaveLength(1));
    expect(grads[0]).toMatchObject({ mint: TOKEN, feedSource: 'helius-ws', venue: 'pumpswap', signature: 'MIGSIG' });
  });

  it('ignores non-migrate and failed pump.fun logs', async () => {
    const { grads, ws } = make([TOKEN, WSOL_MINT]);
    ws.fire('open', {});
    ws.fire('message', { data: logsMsg(['Program log: Instruction: Buy']) }); // not a migrate
    ws.fire('message', { data: logsMsg(['Program log: Instruction: Migrate'], 'x', { InstructionError: [0, 'X'] }) }); // failed
    await new Promise((r) => setTimeout(r, 20));
    expect(grads).toHaveLength(0);
  });

  it('de-dupes the same signature within a connection', async () => {
    const { grads, ws } = make([TOKEN, WSOL_MINT]);
    ws.fire('open', {});
    ws.fire('message', { data: logsMsg(['Instruction: Migrate'], 'DUP') });
    ws.fire('message', { data: logsMsg(['Instruction: Migrate'], 'DUP') });
    await vi.waitFor(() => expect(grads).toHaveLength(1));
  });
});

describe('HeliusWsFeed atlas mode', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    FakeWs.instance = null;
  });

  function makeAtlas(mints: string[]) {
    vi.stubGlobal('WebSocket', FakeWs);
    const grads: FeedGraduation[] = [];
    const health: boolean[] = [];
    const feed = new HeliusWsFeed({
      rpc: fakeRpc(mints),
      httpUrl: 'https://mainnet.helius-rpc.com/?api-key=secret',
      pumpFunProgramId: PROGRAM_IDS.PUMP_FUN,
      reconnectBaseMs: 10,
      reconnectMaxMs: 100,
      atlasEnabled: true,
    });
    feed.onGraduation((g) => grads.push(g));
    feed.onHealth((h) => health.push(h));
    feed.start();
    return { feed, grads, health, ws: FakeWs.instance! };
  }

  function atlasMsg(signature: string, mints: string[]) {
    const balances = mints.map((mint) => ({ mint }));
    return JSON.stringify({
      jsonrpc: '2.0',
      method: 'transactionNotification',
      params: {
        result: {
          slot: 1,
          transaction: {
            signatures: [signature],
            meta: {
              err: null,
              logMessages: ['Program log: Instruction: Migrate'],
              preTokenBalances: [],
              postTokenBalances: balances,
            },
          },
        },
        subscription: 7,
      },
    });
  }

  it('tries transactionSubscribe first when atlas is enabled', () => {
    const { ws } = makeAtlas([TOKEN]);
    ws.fire('open', {});
    expect(ws.sent[0]).toContain('transactionSubscribe');
    expect(ws.sent[0]).toContain(PROGRAM_IDS.PUMP_FUN);
  });

  it('emits the mint inline from the notification with no RPC lookup', async () => {
    let lookups = 0;
    vi.stubGlobal('WebSocket', FakeWs);
    const grads: FeedGraduation[] = [];
    const feed = new HeliusWsFeed({
      rpc: { getTransactionTokenMints: async () => { lookups++; return [TOKEN]; } } as unknown as RpcClient,
      httpUrl: 'https://mainnet.helius-rpc.com/?api-key=secret',
      pumpFunProgramId: PROGRAM_IDS.PUMP_FUN,
      reconnectBaseMs: 10,
      reconnectMaxMs: 100,
      atlasEnabled: true,
    });
    feed.onGraduation((g) => grads.push(g));
    feed.onHealth(() => {});
    feed.start();
    const ws = FakeWs.instance!;
    ws.fire('open', {});
    ws.fire('message', { data: JSON.stringify({ id: 1, result: 7 }) }); // atlas ack
    ws.fire('message', { data: atlasMsg('ATLASIG', [TOKEN, WSOL_MINT]) });
    await vi.waitFor(() => expect(grads).toHaveLength(1));
    expect(grads[0]).toMatchObject({ mint: TOKEN, feedSource: 'helius-ws', signature: 'ATLASIG' });
    expect(lookups).toBe(0);
  });

  it('falls back to logsSubscribe when the server rejects atlas', async () => {
    const { grads, health, ws } = makeAtlas([TOKEN, WSOL_MINT]);
    ws.fire('open', {});
    ws.fire('message', { data: JSON.stringify({ id: 1, error: { code: -32600, message: 'not available on the free plan' } }) });
    expect(ws.sent[1]).toContain('logsSubscribe');
    ws.fire('message', { data: JSON.stringify({ id: 2, result: 9 }) }); // logs ack
    expect(health).toContain(true);
    ws.fire('message', { data: logsMsg(['Program log: Instruction: Migrate'], 'FALLBACKSIG') });
    await vi.waitFor(() => expect(grads).toHaveLength(1));
    expect(grads[0]).toMatchObject({ mint: TOKEN, signature: 'FALLBACKSIG' });
  });
});

describe('extractAtlasTx', () => {
  it('handles nested transaction.transaction.meta with string[] signatures', () => {
    const tx = extractAtlasTx({
      transaction: {
        transaction: {
          signature: ['SIGX'],
          meta: {
            err: null,
            logMessages: ['Instruction: Migrate'],
            preTokenBalances: [{ mint: WSOL_MINT }],
            postTokenBalances: [{ mint: TOKEN }],
          },
        },
      },
    });
    expect(tx).toMatchObject({ signature: 'SIGX', logs: ['Instruction: Migrate'], err: null });
    expect(tx!.mints).toEqual(expect.arrayContaining([TOKEN, WSOL_MINT]));
  });

  it('returns null when there is no transaction content', () => {
    expect(extractAtlasTx({})).toBeNull();
  });
});
