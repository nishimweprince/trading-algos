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
  options: unknown;
  constructor(url: string, options?: unknown) {
    this.url = url;
    this.options = options;
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
  return { getTransactionTokenMints: async () => ({ mints, slot: 777 }) } as unknown as RpcClient;
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
      rpc: { getTransactionTokenMints: async () => { lookups++; return { mints: [TOKEN], slot: 777 }; } } as unknown as RpcClient,
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

describe('HeliusWsFeed endpoint derivation', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    FakeWs.instance = null;
  });

  it('opens the socket with no extra handshake options (key in URL)', () => {
    vi.stubGlobal('WebSocket', FakeWs);
    const feed = new HeliusWsFeed({
      rpc: fakeRpc([TOKEN]),
      httpUrl: 'https://mainnet.helius-rpc.com/?api-key=secret',
      pumpFunProgramId: PROGRAM_IDS.PUMP_FUN,
      reconnectBaseMs: 10,
      reconnectMaxMs: 100,
    });
    feed.onGraduation(() => {});
    feed.onHealth(() => {});
    feed.start();
    const ws = FakeWs.instance!;
    expect(ws.url).toBe('wss://mainnet.helius-rpc.com/?api-key=secret');
    expect(ws.options).toBeUndefined();
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

describe('HeliusWsFeed liveness + narrowing', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.useRealTimers();
    FakeWs.instance = null;
  });

  const AUTH = '39azUYFWPz3VHgKCf3VChUwbpURdCHRxjWVowf5jUJjg';

  function makeNarrowed(opts: { atlas?: boolean } = {}) {
    vi.stubGlobal('WebSocket', FakeWs);
    const grads: FeedGraduation[] = [];
    const health: Array<{ healthy: boolean; detail?: string }> = [];
    const activity: Array<{ kind: string; slot?: number }> = [];
    const feed = new HeliusWsFeed({
      rpc: fakeRpc([TOKEN, WSOL_MINT]),
      httpUrl: 'https://mainnet.helius-rpc.com/?api-key=secret',
      pumpFunProgramId: PROGRAM_IDS.PUMP_FUN,
      reconnectBaseMs: 10,
      reconnectMaxMs: 100,
      atlasEnabled: opts.atlas ?? true,
      migrationAuthority: AUTH,
    });
    feed.onGraduation((g) => grads.push(g));
    feed.onHealth((healthy, detail) => health.push(detail !== undefined ? { healthy, detail } : { healthy }));
    feed.onActivity((a) => activity.push(a.slot !== undefined ? { kind: a.kind, slot: a.slot } : { kind: a.kind }));
    feed.start();
    return { feed, grads, health, activity, ws: FakeWs.instance! };
  }

  it('atlas filter carries accountRequired [pumpFun, authority] and tx version 1', () => {
    const { ws } = makeNarrowed();
    ws.fire('open', {});
    const req = JSON.parse(ws.sent[0]!) as { params: [Record<string, unknown>, Record<string, unknown>] };
    expect(req.params[0]['accountRequired']).toEqual([PROGRAM_IDS.PUMP_FUN, AUTH]);
    expect(req.params[1]['maxSupportedTransactionVersion']).toBe(1);
  });

  it('subscribes to slots after the ack and reports slot ticks as activity', () => {
    const { ws, activity, feed } = makeNarrowed();
    ws.fire('open', {});
    ws.fire('message', { data: JSON.stringify({ id: 1, result: 42 }) });
    expect(ws.sent.some((m) => m.includes('slotSubscribe'))).toBe(true);
    ws.fire('message', { data: JSON.stringify({ id: 3, result: 9 }) });
    ws.fire('message', {
      data: JSON.stringify({ jsonrpc: '2.0', method: 'slotNotification', params: { result: { parent: 99, root: 90, slot: 100 } } }),
    });
    expect(activity.at(-1)).toEqual({ kind: 'slot', slot: 100 });
    expect(feed.liveness).toBe('slot');
  });

  it('degrades liveness to silence when slotSubscribe is rejected', () => {
    const { ws, feed } = makeNarrowed();
    ws.fire('open', {});
    ws.fire('message', { data: JSON.stringify({ id: 1, result: 42 }) });
    ws.fire('message', { data: JSON.stringify({ id: 3, error: { code: -32601, message: 'not supported' } }) });
    expect(feed.liveness).toBe('silence');
  });

  it('stamps slot from the Atlas notification and matches MigrateV2', () => {
    const { ws, grads } = makeNarrowed();
    ws.fire('open', {});
    ws.fire('message', { data: JSON.stringify({ id: 1, result: 42 }) });
    ws.fire('message', {
      data: JSON.stringify({
        jsonrpc: '2.0',
        method: 'transactionNotification',
        params: {
          result: {
            slot: 4242,
            transaction: {
              signatures: ['V2SIG'],
              meta: {
                err: null,
                logMessages: ['Program log: Instruction: MigrateV2'],
                preTokenBalances: [],
                postTokenBalances: [{ mint: TOKEN }, { mint: WSOL_MINT }],
              },
            },
          },
        },
      }),
    });
    expect(grads).toHaveLength(1);
    expect(grads[0]).toMatchObject({ mint: TOKEN, signature: 'V2SIG', slot: 4242 });
  });

  it('stamps slot from context.slot in logs mode (rpc slot as fallback)', async () => {
    const { ws, grads } = makeNarrowed({ atlas: false });
    ws.fire('open', {});
    ws.fire('message', { data: JSON.stringify({ id: 1, result: 42 }) });
    ws.fire('message', {
      data: JSON.stringify({
        jsonrpc: '2.0',
        method: 'logsNotification',
        params: { result: { context: { slot: 555 }, value: { signature: 'LOGSIG', logs: ['Instruction: MigrateV2'], err: null } } },
      }),
    });
    await vi.waitFor(() => expect(grads).toHaveLength(1));
    expect(grads[0]!.slot).toBe(555);
  });

  it('reconnect() abandons the socket without waiting for close and ignores its stale events', () => {
    vi.useFakeTimers();
    const { ws, feed, health } = makeNarrowed();
    ws.fire('open', {});
    ws.fire('message', { data: JSON.stringify({ id: 1, result: 42 }) });
    const first = ws;
    feed.reconnect('test');
    // health false emitted once with the reason; FakeWs.close fires 'close'
    // synchronously — the stale handler must NOT add a second reconnect.
    expect(health.filter((h) => !h.healthy)).toEqual([{ healthy: false, detail: 'test' }]);
    feed.reconnect('again'); // no-op while a reconnect is pending
    expect(health.filter((h) => !h.healthy)).toHaveLength(1);
    vi.advanceTimersByTime(20);
    const second = FakeWs.instance!;
    expect(second).not.toBe(first);
    // Late events from the abandoned socket are ignored.
    first.fire('close', { code: 1006 });
    expect(health.filter((h) => !h.healthy)).toHaveLength(1);
    second.fire('open', {});
    expect(second.sent[0]).toContain('transactionSubscribe');
  });
});
