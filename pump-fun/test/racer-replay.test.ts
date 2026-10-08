/**
 * Replays a recorded window of the racer's live LaserStream subscription
 * (src/racer/record-cli.ts, mainnet 2026-10-05) through the real router,
 * mint resolver, tracker and migrate-tx pricing.
 */
import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';
import { PublicKey } from '@solana/web3.js';
import { canonicalPumpPoolPda } from '@pump-fun/pump-swap-sdk';
import { expandFrame, type CompactFrame } from '../src/racer/replay.ts';
import { routeUpdate, type CurveUpdate } from '../src/racer/stream.ts';
import { MintResolver } from '../src/racer/mintResolver.ts';
import { RacerTracker, type TrackerEvent } from '../src/racer/tracker.ts';
import { migrateEntry, type MigrateTx } from '../src/racer/migrateTx.ts';

const FIXTURE = new URL('./fixtures/racer-stream-2026-10-05.jsonl', import.meta.url);
const frames = readFileSync(FIXTURE, 'utf8')
  .split('\n')
  .filter(Boolean)
  .map((l) => JSON.parse(l) as CompactFrame);

let cached: ReturnType<typeof runReplay> | null = null;
function replay() {
  return (cached ??= runReplay());
}

async function runReplay() {
  // Stand-in for getTransaction: every mint the window names anywhere.
  const known = new Set<string>();
  for (const f of frames) {
    if (f.t === 'c') f.mints.forEach((m) => known.add(m));
    if (f.t === 'x') [...f.pre, ...f.post].forEach((b) => known.add(b.mint));
  }
  const resolver = new MintResolver<CurveUpdate>(async () => [], {}, async () => {});
  // getTransaction stand-in: every curve whose mint the window names is resolvable.
  for (const m of known) resolver.noteMint(m);
  const events: TrackerEvent[] = [];
  const tracker = new RacerTracker(
    { admitCostSol: 5, maxCostSol: 0.5, feeBps: 125, trackWindowMs: 7_200_000, completeNoPoolMs: 120_000, maxTracked: 500 },
    (e) => void events.push(e),
  );
  const txs: MigrateTx[] = [];
  const handled = new Set<string>();
  for (const f of frames) {
    for (const update of expandFrame(f)) {
      routeUpdate(update, {
        onLaunch: (m) => void resolver.noteMint(m),
        onCurve: (u) => resolver.resolve(u.curve, u.signature, u, (mint, upd) => tracker.onCurve(mint, upd.curve, upd.reserves, upd.slot)),
        onMigrateTx: (tx) => {
          txs.push(tx);
          if (!tx.mint) return;
          resolver.noteMint(tx.mint);
          if (!tx.landed || handled.has(tx.mint)) return;
          handled.add(tx.mint);
          tracker.onMigrate(tx.mint, tx.slot);
        },
      });
    }
  }
  return { events, txs, resolver };
}

describe('racer replay (recorded mainnet window)', () => {
  it('the fixture holds curve writes, creates and migrate attempts', () => {
    const kinds = new Set(frames.map((f) => f.t));
    expect(kinds).toEqual(new Set(['a', 'c', 'x']));
  });

  it('emits exactly one migrated per mint with a landed migrate tx', async () => {
    const { events, txs } = await replay();
    const landedMints = new Set(txs.filter((t) => t.landed && t.mint).map((t) => t.mint));
    const migrated = events.filter((e) => e.kind === 'migrated');
    expect(migrated.length).toBe(landedMints.size);
    expect(new Set(migrated.map((e) => (e.kind === 'migrated' ? e.mint : ''))).size).toBe(migrated.length);
    // Never more than one landed migrate per mint.
    expect(txs.filter((t) => t.landed).length).toBe(landedMints.size);
  });

  it('every landed migrate prices off the canonical pool vaults', async () => {
    const { txs } = await replay();
    const landed = txs.filter((t) => t.landed && t.mint);
    expect(landed.length).toBeGreaterThan(0);
    for (const t of landed) {
      const pool = canonicalPumpPoolPda(new PublicKey(t.mint!)).toBase58();
      const e = migrateEntry(t, t.mint!, pool, 0n);
      expect(e, t.signature).not.toBeNull();
      expect(e!.reach.price).toBeGreaterThan(0);
      if (!t.poolBuyAfter) expect(e!.open).toEqual(e!.reach);
    }
  });

  it('a tracked curve completes at or before its migrate slot', async () => {
    const { events } = await replay();
    const tracked = events.filter((e): e is Extract<TrackerEvent, { kind: 'migrated' }> => e.kind === 'migrated' && e.state !== null);
    for (const e of tracked) {
      if (e.state!.completeSlot === null || e.migrateSlot === null) continue;
      expect(e.migrateSlot - e.state!.completeSlot).toBeGreaterThanOrEqual(0);
      expect(e.migrateSlot - e.state!.completeSlot).toBeLessThan(50);
    }
  });
});
