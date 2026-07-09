import { describe, it, expect, vi, afterEach } from 'vitest';
import { HeliusWsFeed } from '../src/detector/heliusWs.ts';
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
