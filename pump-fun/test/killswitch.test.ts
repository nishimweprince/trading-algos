import { describe, it, expect, vi, afterEach } from 'vitest';
import { mkdtempSync, writeFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { KillFileWatcher } from '../src/risk/killswitch.ts';
import { TypedBus } from '../src/core/bus.ts';

describe('KillFileWatcher', () => {
  const dirs: string[] = [];
  afterEach(() => {
    vi.useRealTimers();
    for (const d of dirs.splice(0)) rmSync(d, { recursive: true, force: true });
  });

  it('emits killSwitch once when the KILL file appears (edge-triggered)', () => {
    vi.useFakeTimers();
    const dir = mkdtempSync(join(tmpdir(), 'kill-'));
    dirs.push(dir);
    const path = join(dir, 'KILL');
    const bus = new TypedBus();
    const events: string[] = [];
    bus.on('killSwitch', (k) => events.push(k.source));

    const w = new KillFileWatcher({ bus, path, pollMs: 100 });
    w.start();
    vi.advanceTimersByTime(250); // no file yet
    expect(events).toEqual([]);

    writeFileSync(path, 'stop');
    vi.advanceTimersByTime(100); // one poll sees it
    vi.advanceTimersByTime(300); // subsequent polls do not re-emit
    expect(events).toEqual(['file']);
    w.stop();
  });
});
