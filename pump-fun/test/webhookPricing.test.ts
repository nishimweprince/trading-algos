import { afterEach, describe, expect, it } from 'vitest';
import { ConfigSchema } from '../src/config/schema.ts';
import { openDb } from '../src/persistence/db.ts';
import { createDashboardApp } from '../src/dashboard/server.ts';
import {
  parseHeliusWebhook,
  WebhookPriceIngest,
} from '../src/positions/webhookPricing.ts';
import type { PoolRef, PriceTick } from '../src/positions/pricing.ts';

const SECRET_ENV = 'HELIUS_WEBHOOK_TEST_SECRET';

afterEach(() => {
  delete process.env[SECRET_ENV];
});

function ref(): PoolRef {
  return { mint: 'MINT', baseVault: 'BASE', quoteVault: 'QUOTE', baseDecimals: 6, creatorAta: 'CREATOR' };
}

function webhookBody(base: string, quote: string, creator?: string) {
  const changes = [
    { userAccount: 'BASE', rawTokenAmount: { tokenAmount: base } },
    { userAccount: 'QUOTE', rawTokenAmount: { tokenAmount: quote } },
  ];
  if (creator !== undefined) changes.push({ userAccount: 'CREATOR', rawTokenAmount: { tokenAmount: creator } });
  return [{ accountData: [{ account: 'X', tokenBalanceChanges: changes }] }];
}

describe('parseHeliusWebhook', () => {
  it('extracts vault balances from array and single-object payloads', () => {
    const fromArray = parseHeliusWebhook(webhookBody('1000', '2000'));
    expect(fromArray.get('BASE')).toBe(1000n);
    expect(fromArray.get('QUOTE')).toBe(2000n);
    const single = parseHeliusWebhook(webhookBody('5', '6')[0]);
    expect(single.get('BASE')).toBe(5n);
  });

  it('tolerates numbers and skips malformed entries', () => {
    const out = parseHeliusWebhook([
      { accountData: [{ tokenBalanceChanges: [{ userAccount: 'A', rawTokenAmount: { tokenAmount: 42 } }] }] },
      { accountData: [{ tokenBalanceChanges: [{ userAccount: 'B' }] }] },
      { nope: true },
      null,
    ]);
    expect(out.get('A')).toBe(42n);
    expect(out.has('B')).toBe(false);
    expect(out.size).toBe(1);
  });
});

describe('WebhookPriceIngest', () => {
  it('emits ticks only for pools with all vaults present', () => {
    const ingest = new WebhookPriceIngest(() => 1234);
    const ticks: PriceTick[] = [];
    ingest.register(ref(), (t) => ticks.push(t));
    ingest.register({ mint: 'OTHER', baseVault: 'B2', quoteVault: 'Q2', baseDecimals: 6 }, () => {
      throw new Error('must not fire');
    });
    const emitted = ingest.ingest(parseHeliusWebhook(webhookBody('1000000', '2000000000', '7')));
    expect(emitted).toHaveLength(1);
    expect(ticks).toHaveLength(1);
    expect(ticks[0]).toMatchObject({ mint: 'MINT', baseReserve: 1000000n, quoteReserveLamports: 2000000000n, atMs: 1234, creatorBaseBalance: 7n });
    expect(ticks[0]!.price).toBeGreaterThan(0);
    expect(ingest.stats).toMatchObject({ tracked: 2, payloads: 1, ticks: 1 });
  });

  it('unregister stops delivery', () => {
    const ingest = new WebhookPriceIngest();
    let n = 0;
    ingest.register(ref(), () => n++);
    ingest.unregister('MINT');
    expect(ingest.ingest(parseHeliusWebhook(webhookBody('1', '2')))).toHaveLength(0);
    expect(n).toBe(0);
  });
});

describe('webhook route', () => {
  function appWith(secret: string | undefined, enabled: boolean) {
    if (secret !== undefined) process.env[SECRET_ENV] = secret;
    const config = ConfigSchema.parse({
      dashboard: { enabled: true, host: '127.0.0.1', port: 8787 },
      webhooks: { enabled, secretEnvVar: SECRET_ENV },
    });
    const db = openDb({ path: ':memory:', memory: true });
    const ingest = new WebhookPriceIngest();
    const app = createDashboardApp({ config, db, priceIngest: ingest });
    return { app, db, ingest };
  }

  it('404s when webhooks are disabled', async () => {
    const { app, db } = appWith('s3cret', false);
    const res = await app.request('/api/webhooks/helius', {
      method: 'POST',
      headers: { 'x-webhook-secret': 's3cret', 'content-type': 'application/json' },
      body: JSON.stringify(webhookBody('1', '2')),
    });
    expect(res.status).toBe(404);
    db.close();
  });

  it('401s on a wrong secret and 200s on the right one, driving ticks', async () => {
    const { app, db, ingest } = appWith('s3cret', true);
    const ticks: PriceTick[] = [];
    ingest.register(ref(), (t) => ticks.push(t));

    const bad = await app.request('/api/webhooks/helius', {
      method: 'POST',
      headers: { 'x-webhook-secret': 'wrong', 'content-type': 'application/json' },
      body: JSON.stringify(webhookBody('1', '2')),
    });
    expect(bad.status).toBe(401);
    expect(ticks).toHaveLength(0);

    const good = await app.request('/api/webhooks/helius', {
      method: 'POST',
      headers: { authorization: 'Bearer s3cret', 'content-type': 'application/json' },
      body: JSON.stringify(webhookBody('1000000', '3000000000')),
    });
    expect(good.status).toBe(200);
    expect(await good.json()).toMatchObject({ ok: true, pools: 1 });
    expect(ticks).toHaveLength(1);
    expect(ticks[0]!.mint).toBe('MINT');
    db.close();
  });
});
