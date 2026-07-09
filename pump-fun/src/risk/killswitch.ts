import { existsSync } from 'node:fs';
import { resolve } from 'node:path';
import type { TypedBus } from '../core/bus.ts';
import { logger } from '../core/logger.ts';

/**
 * File-sentinel kill switch (Section 8). Polls for a `KILL` file and emits a
 * single `killSwitch` event when it first appears (edge-triggered). Polling
 * rather than fs.watch — trivial, portable, and one file check per second is
 * nothing. The risk manager latches on the event and blocks entries; the
 * position manager flattens open positions.
 */
export class KillFileWatcher {
  private readonly bus: TypedBus;
  private readonly path: string;
  private readonly pollMs: number;
  private readonly log = logger.child({ mod: 'killswitch' });
  private timer: NodeJS.Timeout | null = null;
  private present = false;

  constructor(deps: { bus: TypedBus; path?: string; pollMs?: number }) {
    this.bus = deps.bus;
    this.path = resolve(deps.path ?? 'KILL');
    this.pollMs = deps.pollMs ?? 1000;
  }

  start(): void {
    if (this.timer) return;
    // Seed current state so a KILL file already present at boot triggers once.
    this.present = false;
    this.timer = setInterval(() => this.poll(), this.pollMs);
    this.log.info('kill-file watcher started', { path: this.path });
  }

  stop(): void {
    if (this.timer) clearInterval(this.timer);
    this.timer = null;
  }

  private poll(): void {
    const exists = existsSync(this.path);
    if (exists && !this.present) {
      this.present = true;
      this.log.error('KILL file detected — engaging kill switch', { path: this.path });
      this.bus.emit('killSwitch', { source: 'file', detail: this.path });
    } else if (!exists && this.present) {
      // Reset the edge so re-creating the file re-triggers (still latched in risk
      // manager until restart — this only re-arms the file edge).
      this.present = false;
    }
  }
}
