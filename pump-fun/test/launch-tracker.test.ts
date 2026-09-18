import { describe, it, expect, vi, afterEach } from 'vitest';
import { LaunchTracker } from '../src/guardrails/launchTracker.ts';
import { openDb } from '../src/persistence/db.ts';
import { Repositories } from '../src/persistence/repositories.ts';
import type { FeedLaunch } from '../src/core/types.ts';
import type { RpcClient } from '../src/core/rpc.ts';

const FRESH_DATA =
  'F7f4N2DYrGDUF053erMDALdfwTEHAAAA1H87K+m0AgC3s501AAAAAACAxqR+jQMAAMEp0sixukwfAbJ6JX39jcKJ9dqdN7lUxvMtnNWLAmHeAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=';
const GRAD_DATA =
  'F7f4N2DYrGAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAACAxqR+jQMAAReF/axSlr8h7xFKBPsemakreHzVhv2jhkIhnU51IdmTAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA==';
// Valid discriminator, zero reserves, complete=0 (brand-new curve, unpriced).
const EMPTY_DATA = Buffer.concat([
  Buffer.from('17b7f83760d8ac60', 'hex'),
  Buffer.alloc(40),
  Buffer.from([0]),
]).toString('base64');

const M1 = '7v4shBJmb73embNcid1dMQhZFhNTpBhtFPMFTm73b4bv';
const M2 = 'AJQ48erLGxjwqFAZR1vm9HBU518ArDY9iU6sceaVpump';
const M3 = '9HB7uiNQeWTGG1tkuGaMQLhdt9Lg6vmJ4vtLJc1Wpump';

function launch(mint: string): FeedLaunch {
  return { mint, feedSource: 'pumpportal', receivedAtNs: 1n };
}

// Ordered fake: returns fixture per call position (tests adopt one mint at a time).
function orderedRpc(data: Array<string | null>): RpcClient {
  let calls = 0;
  return {
    getMultipleAccountsBase64: async (pubkeys: string[]) =>
      pubkeys.map(() => {
        const d = data[Math.min(calls++, data.length - 1)];
        return d === null ? null : { data: d, owner: '6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P' };
      }),
  } as unknown as RpcClient;
}

function setup(rpc: RpcClient, opts: { maxConcurrent?: number; windowMs?: number } = {}) {
  vi.useFakeTimers();
  vi.setSystemTime(1_000_000);
  const db = openDb({ path: ':memory:', memory: true });
  const repos = new Repositories(db);
  const tracker = new LaunchTracker(rpc, repos, {
    pollMs: 10_000,
    maxConcurrent: opts.maxConcurrent ?? 10,
    windowMs: opts.windowMs ?? 2 * 60 * 60_000,
    now: () => Date.now(),
  });
  return { db, repos, tracker };
}

const openTracks = (db: { prepare(s: string): { all(): Array<unknown> } }) =>
  db.prepare('SELECT mint, baseline_price, peak_price, graduated, closed_at FROM launch_tracks').all();

describe('LaunchTracker (S1 paper)', () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it('adopts newest-first up to the cap and counts the skipped backlog', async () => {
    const { db, repos, tracker } = setup(orderedRpc([FRESH_DATA]), { maxConcurrent: 1 });
    repos.recordLaunch(launch(M1));
    repos.recordLaunch(launch(M2));
    await tracker.tick();
    expect(tracker.size).toBe(1);
    expect(tracker.skipped).toBe(1);
    // Newest-first: M2 adopted (inserted later), M1 left untracked.
    expect(openTracks(db)).toHaveLength(1);
    await tracker.stop();
  });

  it('sets baseline on the first priced read and tracks the peak', async () => {
    const { db, repos, tracker } = setup(orderedRpc([FRESH_DATA, FRESH_DATA]));
    repos.recordLaunch(launch(M1));
    await tracker.tick();
    await tracker.tick();
    const rows = openTracks(db) as Array<{ baseline_price: number; peak_price: number; closed_at: null }>;
    expect(rows).toHaveLength(1);
    expect(rows[0]!.baseline_price).toBeGreaterThan(0);
    expect(rows[0]!.peak_price).toBeGreaterThanOrEqual(rows[0]!.baseline_price);
    expect(rows[0]!.closed_at).toBeNull();
    await tracker.stop();
  });

  it('leaves baseline unset on zero-reserve curves without closing', async () => {
    const { db, repos, tracker } = setup(orderedRpc([EMPTY_DATA]));
    repos.recordLaunch(launch(M1));
    await tracker.tick();
    const rows = openTracks(db) as Array<{ baseline_price: null; closed_at: null }>;
    expect(rows).toHaveLength(1);
    expect(rows[0]!.baseline_price).toBeNull();
    expect(rows[0]!.closed_at).toBeNull();
    await tracker.stop();
  });

  it('closes graduated when the mint hits the graduations table, with MFE', async () => {
    const { db, repos, tracker } = setup(orderedRpc([FRESH_DATA, FRESH_DATA]));
    repos.recordLaunch(launch(M1));
    await tracker.tick();
    repos.recordGraduation({
      mint: M1,
      venue: 'pumpswap',
      poolAddress: '',
      slot: 1,
      feedSource: 'pumpportal',
      receivedAtNs: 2n,
    });
    await tracker.tick();
    expect(tracker.size).toBe(0);
    const closed = db
      .prepare('SELECT graduated, peak_mfe_pct, baseline_price FROM launch_tracks WHERE closed_at IS NOT NULL')
      .all() as Array<{ graduated: number; peak_mfe_pct: number; baseline_price: number }>;
    expect(closed).toHaveLength(1);
    expect(closed[0]!.graduated).toBe(1);
    expect(closed[0]!.peak_mfe_pct).toBeGreaterThanOrEqual(0);
    // Table separation: no positions, no candidates.
    expect(db.prepare('SELECT COUNT(*) AS n FROM positions').get()).toEqual({ n: 0 });
    expect(db.prepare('SELECT COUNT(*) AS n FROM candidates').get()).toEqual({ n: 0 });
    await tracker.stop();
  });

  it('closes on the curve complete flag and expires past the window', async () => {
    const { repos, tracker } = setup(orderedRpc([GRAD_DATA]), { windowMs: 60_000 });
    repos.recordLaunch(launch(M2));
    await tracker.tick(); // complete flag closes immediately (unpriced graduation still counts)
    expect(tracker.size).toBe(0);
    await tracker.stop();

    const t2 = setup(orderedRpc([FRESH_DATA]), { windowMs: 60_000 });
    t2.repos.recordLaunch(launch(M3));
    await t2.tracker.tick();
    expect(t2.tracker.size).toBe(1);
    vi.setSystemTime(1_000_000 + 61_000);
    await t2.tracker.tick();
    expect(t2.tracker.size).toBe(0);
    await t2.tracker.stop();
  });
});
