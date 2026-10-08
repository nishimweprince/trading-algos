import type { CheckResult } from '../core/types.ts';
import { logger } from '../core/logger.ts';
import type { Repositories } from '../persistence/repositories.ts';
import type { CheckContext } from './engine.ts';

/**
 * Racer gate (2026-10-06): the trader only enters migrations whose bonding
 * curve the racer worker would have fired on — completion cost at or under
 * racer.maxCompletionCostSol while the curve was still open. The racer writes
 * racer_curves.fire_slot synchronously when it fires; this process reads it.
 *
 * Latency: R1 is one primary-key SQLite read with a cached statement (µs),
 * run at verdict time — after the fast read — so a fire the racer persisted
 * in the migrate slot itself (~8 % of fired migrations) is still seen. No
 * network call is added to the buy path.
 *
 * Fails closed: a migration the racer never fired on (or a racer worker that
 * is down) means no entry.
 */
export function checkRacerFire(ctx: CheckContext): CheckResult {
  const id = 'R1';
  const label = 'Racer would-fire';
  const slot = ctx.repos.racerFireSlot(ctx.candidate.graduation.mint);
  if (slot === null) return { id, label, status: 'fail', reason: 'racer_not_fired', detail: 'racer did not fire on this curve' };
  return { id, label, status: 'pass', detail: `racer fired at slot ${slot}` };
}

/**
 * Pre-arms fired curves. Fire → migrate is 3+ slots for ~88 % of fired
 * migrations, so subscribing to the pool PDA at fire time (instead of at
 * curve completion, which mostly lands with the migrate itself) lets the
 * migrate's pool write be pushed and the verdict skip its RPC read.
 * Polls an index-backed query of open fired curves (a handful of rows).
 */
export class RacerFireFeed {
  private readonly repos: Pick<Repositories, 'openRacerFires'>;
  private readonly pollMs: number;
  private readonly onFire: (mint: string) => void;
  private readonly seen = new Set<string>();
  private timer: NodeJS.Timeout | null = null;
  private readonly log = logger.child({ mod: 'racer-gate' });

  constructor(opts: { repos: Pick<Repositories, 'openRacerFires'>; pollMs: number; onFire: (mint: string) => void }) {
    this.repos = opts.repos;
    this.pollMs = opts.pollMs;
    this.onFire = opts.onFire;
  }

  start(): void {
    if (this.timer) return;
    this.timer = setInterval(() => this.poll(), this.pollMs);
    this.timer.unref?.();
    this.poll();
  }

  stop(): void {
    if (this.timer) clearInterval(this.timer);
    this.timer = null;
  }

  poll(): void {
    let open: string[];
    try {
      open = this.repos.openRacerFires();
    } catch (err) {
      this.log.debug('racer fire poll failed', { err });
      return;
    }
    for (const mint of open) {
      if (this.seen.has(mint)) continue;
      this.seen.add(mint);
      this.onFire(mint);
    }
    // Closed curves drop out of `open`; keep `seen` bounded to what is open.
    if (this.seen.size > open.length + 1_000) {
      const live = new Set(open);
      for (const m of this.seen) if (!live.has(m)) this.seen.delete(m);
    }
  }
}
