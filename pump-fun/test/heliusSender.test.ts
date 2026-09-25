import { describe, it, expect } from 'vitest';
import {
  HeliusSenderTxSender,
  HELIUS_SENDER_TIP_ACCOUNTS,
  heliusSenderUrl,
  randomSenderTipAccount,
} from '../src/executor/heliusSender.ts';
import { buildSenderTipLamports } from '../src/executor/fees.ts';
import { ComputeUnitTracker } from '../src/executor/computeUnits.ts';
import { ConfigSchema } from '../src/config/schema.ts';

function cfg(heliusSender: Record<string, unknown> = {}) {
  return ConfigSchema.parse({ mode: 'paper', heliusSender });
}

function jsonFetch(body: unknown, ok = true): typeof fetch {
  return (async () => ({ ok, status: ok ? 200 : 500, json: async () => body })) as unknown as typeof fetch;
}

describe('Helius Sender (P4.1)', () => {
  it('is off by default', () => {
    expect(cfg().heliusSender.enabled).toBe(false);
  });

  it('rejects a Max-mode min tip below the documented 0.001 SOL', () => {
    expect(() => cfg({ swqosOnly: false, minTipLamports: 5_000 })).toThrow(/minTipLamports/);
    expect(cfg({ swqosOnly: false, minTipLamports: 1_000_000 }).heliusSender.swqosOnly).toBe(false);
  });

  it('builds the endpoint URL with swqos_only and api key', () => {
    const u = new URL(heliusSenderUrl('https://sender.helius-rpc.com/fast', { swqosOnly: true, apiKey: 'k' }));
    expect(u.searchParams.get('swqos_only')).toBe('true');
    expect(u.searchParams.get('api-key')).toBe('k');
    expect(new URL(heliusSenderUrl('https://sender.helius-rpc.com/fast', { swqosOnly: false })).search).toBe('');
  });

  it('picks tip accounts from the Sender set', () => {
    expect(randomSenderTipAccount(() => 0)).toBe(HELIUS_SENDER_TIP_ACCOUNTS[0]);
    expect(randomSenderTipAccount(() => 0.9999)).toBe(HELIUS_SENDER_TIP_ACCOUNTS.at(-1));
  });

  it('sends base64 with skipPreflight and returns the signature', async () => {
    let sent: { method: string; params: [string, Record<string, unknown>] } | undefined;
    const fetchImpl = (async (_url: string, init: { body: string }) => {
      sent = JSON.parse(init.body);
      return { ok: true, status: 200, json: async () => ({ result: 'SIG' }) };
    }) as unknown as typeof fetch;
    const s = new HeliusSenderTxSender({ url: 'https://sender.helius-rpc.com/fast', swqosOnly: true, fetchImpl });
    await expect(s.send(new Uint8Array([1, 2, 3]))).resolves.toEqual({ signature: 'SIG' });
    expect(sent?.method).toBe('sendTransaction');
    expect(sent?.params[0]).toBe(Buffer.from([1, 2, 3]).toString('base64'));
    expect(sent?.params[1]).toMatchObject({ encoding: 'base64', skipPreflight: true, maxRetries: 0 });
  });

  it('surfaces JSON-RPC and HTTP errors, and refuses to simulate', async () => {
    const rpcErr = new HeliusSenderTxSender({ url: 'https://x.test', swqosOnly: true, fetchImpl: jsonFetch({ error: { code: -1, message: 'tip too low' } }) });
    await expect(rpcErr.send(new Uint8Array([1]))).rejects.toThrow(/tip too low/);
    const httpErr = new HeliusSenderTxSender({ url: 'https://x.test', swqosOnly: true, fetchImpl: jsonFetch({}, false) });
    await expect(httpErr.send(new Uint8Array([1]))).rejects.toThrow(/HTTP 500/);
    await expect(rpcErr.simulate()).rejects.toThrow(/does not simulate/);
  });
});

describe('buildSenderTipLamports', () => {
  it('SWQoS-only pays the flat minimum', async () => {
    expect(await buildSenderTipLamports(cfg({ enabled: true }), jsonFetch([]))).toBe(5_000);
  });

  it('Max mode bids the tip-floor percentile plus buffer, clamped', async () => {
    const c = cfg({ enabled: true, swqosOnly: false, minTipLamports: 1_000_000, tipPercentile: 75, tipBufferPct: 10 });
    // p75 0.0015 SOL x 1.1 = 1,650,000 lamports
    expect(await buildSenderTipLamports(c, jsonFetch([{ landed_tips_75th_percentile: 0.0015 }]))).toBe(1_650_000);
    // below min -> min; above cap -> cap
    expect(await buildSenderTipLamports(c, jsonFetch([{ landed_tips_75th_percentile: 0.00001 }]))).toBe(1_000_000);
    expect(await buildSenderTipLamports(c, jsonFetch([{ landed_tips_75th_percentile: 1 }]))).toBe(2_000_000);
  });

  it('falls back to the minimum when the tip floor is unavailable', async () => {
    const c = cfg({ enabled: true, swqosOnly: false, minTipLamports: 1_000_000 });
    expect(await buildSenderTipLamports(c, jsonFetch([], false))).toBe(1_000_000);
    expect(await buildSenderTipLamports(c, jsonFetch([{}]))).toBe(1_000_000);
    const throwing = (async () => {
      throw new Error('net');
    }) as unknown as typeof fetch;
    expect(await buildSenderTipLamports(c, throwing)).toBe(1_000_000);
  });
});

describe('ComputeUnitTracker', () => {
  it('uses the flat default until enough samples, then max x 1.15 within bounds', () => {
    const t = new ComputeUnitTracker();
    for (let i = 0; i < 4; i++) t.record('buy', 100_000);
    expect(t.limitFor('buy')).toBe(250_000);
    t.record('buy', 120_000);
    expect(t.limitFor('buy')).toBe(138_000);
    expect(t.limitFor('sell')).toBe(250_000);
  });

  it('ignores missing samples and clamps to floor/cap', () => {
    const t = new ComputeUnitTracker({ minSamples: 1 });
    t.record('a', undefined);
    t.record('a', 0);
    expect(t.limitFor('a')).toBe(250_000);
    t.record('a', 10_000);
    expect(t.limitFor('a')).toBe(60_000);
    t.record('b', 1_000_000);
    expect(t.limitFor('b')).toBe(400_000);
  });

  it('keeps a rolling window', () => {
    const t = new ComputeUnitTracker({ window: 2, minSamples: 1, marginPct: 0 });
    t.record('k', 300_000);
    t.record('k', 100_000);
    t.record('k', 100_000);
    expect(t.limitFor('k')).toBe(100_000);
  });
});
