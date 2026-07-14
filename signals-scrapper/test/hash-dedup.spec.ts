import { mkdtempSync, readFileSync, rmSync, writeFileSync } from 'fs';
import { tmpdir } from 'os';
import { join } from 'path';
import { computeIdeaHash } from '../src/dedup/hash';
import { SeenStore } from '../src/dedup/seen-store';
import { TradingIdea } from '../src/models/trading-idea.model';

function baseIdea(overrides: Partial<TradingIdea> = {}): TradingIdea {
  const partial = {
    provider: 'TRADING_CENTRAL' as const,
    instrument: 'USD/CAD',
    timeframe: '30 MIN',
    direction: 'DOWN' as const,
    target: 1.4115,
    ideaTimestamp: '2026-07-10T17:10:00.000Z',
    capturedAt: '2026-07-10T17:20:00.000Z',
    sourceUrl: 'https://example.com',
    ...overrides,
  };
  const hash = overrides.hash ?? computeIdeaHash(partial);
  return { ...partial, hash };
}

describe('computeIdeaHash', () => {
  it('is deterministic for the same inputs', () => {
    const a = computeIdeaHash({
      provider: 'TRADING_CENTRAL',
      instrument: 'USD/CAD',
      timeframe: '30 MIN',
      ideaTimestamp: '2026-07-10T17:10:00.000Z',
      direction: 'DOWN',
      target: 1.4115,
    });
    const b = computeIdeaHash({
      provider: 'TRADING_CENTRAL',
      instrument: 'USD/CAD',
      timeframe: '30 MIN',
      ideaTimestamp: '2026-07-10T17:10:00.000Z',
      direction: 'DOWN',
      target: 1.4115,
    });
    expect(a).toBe(b);
    expect(a).toMatch(/^[a-f0-9]{64}$/);
  });

  it('changes when target changes (updated idea counts as new)', () => {
    const h1 = computeIdeaHash({
      provider: 'TRADING_CENTRAL',
      instrument: 'USD/CAD',
      timeframe: '30 MIN',
      ideaTimestamp: '2026-07-10T17:10:00.000Z',
      direction: 'DOWN',
      target: 1.4115,
    });
    const h2 = computeIdeaHash({
      provider: 'TRADING_CENTRAL',
      instrument: 'USD/CAD',
      timeframe: '30 MIN',
      ideaTimestamp: '2026-07-10T17:10:00.000Z',
      direction: 'DOWN',
      target: 1.41,
    });
    expect(h1).not.toBe(h2);
  });

  it('changes when direction changes', () => {
    const h1 = computeIdeaHash({
      provider: 'AUTOCHARTIST',
      instrument: 'EUR/USD',
      timeframe: '1 H',
      ideaTimestamp: '2026-07-10T17:10:00.000Z',
      direction: 'UP',
      target: 1.1,
    });
    const h2 = computeIdeaHash({
      provider: 'AUTOCHARTIST',
      instrument: 'EUR/USD',
      timeframe: '1 H',
      ideaTimestamp: '2026-07-10T17:10:00.000Z',
      direction: 'DOWN',
      target: 1.1,
    });
    expect(h1).not.toBe(h2);
  });

  it('matches formula: sha256(provider|instrument|timeframe|ideaTimestamp|direction|target)', () => {
    const { createHash } = require('crypto') as typeof import('crypto');
    const expected = createHash('sha256')
      .update(
        'TRADING_CENTRAL|USD/CAD|30 MIN|2026-07-10T17:10:00.000Z|DOWN|1.4115',
        'utf8',
      )
      .digest('hex');
    const actual = computeIdeaHash({
      provider: 'TRADING_CENTRAL',
      instrument: 'USD/CAD',
      timeframe: '30 MIN',
      ideaTimestamp: '2026-07-10T17:10:00.000Z',
      direction: 'DOWN',
      target: 1.4115,
    });
    expect(actual).toBe(expected);
  });

  it('uses stable stop-loss/take-profit fallback when OCR timestamp is missing', () => {
    const first = {
      provider: 'TRADING_CENTRAL',
      instrument: 'AUD/JPY',
      timeframe: '30 MIN',
      ideaTimestamp: null,
      direction: 'UP',
      entry: 112.47,
      stopLoss: 112.17,
      takeProfit: 113.06,
    } as const;
    const second = {
      ...first,
      entry: 112.52,
    } as const;
    const a = computeIdeaHash(first);
    const b = computeIdeaHash(second);
    expect(a).toBe(b);
  });
});

describe('SeenStore', () => {
  let dir: string;
  let path: string;

  beforeEach(() => {
    dir = mkdtempSync(join(tmpdir(), 'seen-store-'));
    path = join(dir, 'seen.json');
  });

  afterEach(() => {
    rmSync(dir, { recursive: true, force: true });
  });

  it('loads empty when file missing', () => {
    const store = new SeenStore(path, 100);
    store.load();
    expect(store.size()).toBe(0);
    expect(store.has('abc')).toBe(false);
  });

  it('persists and reloads hashes (restart simulation)', () => {
    const store1 = new SeenStore(path, 100);
    store1.load();
    const idea = baseIdea();
    store1.add(idea.hash, idea.capturedAt);
    store1.save();

    const onDisk = JSON.parse(readFileSync(path, 'utf8')) as {
      hashes: Record<string, string>;
    };
    expect(onDisk.hashes[idea.hash]).toBeDefined();

    const store2 = new SeenStore(path, 100);
    store2.load();
    expect(store2.has(idea.hash)).toBe(true);
    expect(store2.size()).toBe(1);
  });

  it('prunes to maxEntries keeping newest', () => {
    const store = new SeenStore(path, 3);
    store.load();
    store.add('h1', '2026-01-01T00:00:00.000Z');
    store.add('h2', '2026-01-02T00:00:00.000Z');
    store.add('h3', '2026-01-03T00:00:00.000Z');
    store.add('h4', '2026-01-04T00:00:00.000Z');
    store.prune();
    expect(store.size()).toBe(3);
    expect(store.has('h1')).toBe(false);
    expect(store.has('h2')).toBe(true);
    expect(store.has('h4')).toBe(true);
  });

  it('save creates parent directories', () => {
    const nested = join(dir, 'a', 'b', 'seen.json');
    const store = new SeenStore(nested, 10);
    store.load();
    store.add('x', new Date().toISOString());
    store.save();
    expect(readFileSync(nested, 'utf8')).toContain('x');
  });

  it('loads pre-written seen file', () => {
    writeFileSync(
      path,
      JSON.stringify({
        hashes: { preexisting: '2026-01-01T00:00:00.000Z' },
        updatedAt: '2026-01-01T00:00:00.000Z',
      }),
    );
    const store = new SeenStore(path, 10);
    store.load();
    expect(store.has('preexisting')).toBe(true);
  });

  it('migrates a v1 hash-only file to version 2 without losing hashes', () => {
    writeFileSync(
      path,
      JSON.stringify({
        hashes: { legacy: '2026-01-01T00:00:00.000Z' },
        updatedAt: '2026-01-01T00:00:00.000Z',
      }),
    );
    const store = new SeenStore(path, 10, 2);
    store.load();
    store.save();
    const saved = JSON.parse(readFileSync(path, 'utf8'));
    expect(saved.version).toBe(2);
    expect(saved.hashes.legacy).toBeDefined();
    expect(saved.signals).toEqual({});
    expect(saved.runs).toEqual([]);
  });

  it('stores full signals and bounds failure/success debug history', () => {
    const store = new SeenStore(path, 10, 2);
    store.load();
    const idea = baseIdea();
    store.addIdeas([idea], idea.capturedAt);
    for (let i = 0; i < 3; i++) {
      store.addRun({
        id: `run-${i}`,
        provider: 'TRADING_CENTRAL',
        sourceUrl: idea.sourceUrl,
        capturedAt: idea.capturedAt,
        status: i === 0 ? 'failed' : 'success',
        stage: i === 0 ? 'ocr' : 'complete',
        error: i === 0 ? 'failed' : undefined,
      });
    }
    store.save();
    expect(store.allSignals()[idea.hash].signal.instrument).toBe('USD/CAD');
    expect(store.allRuns().map((run) => run.id)).toEqual(['run-1', 'run-2']);
  });
});
