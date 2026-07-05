import { describe, it, expect, afterEach } from 'vitest';
import { mkdtempSync, writeFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { acquireLock, LockError } from '../src/core/lock.ts';

describe('single-instance lock', () => {
  const dirs: string[] = [];
  const tmp = () => {
    const d = mkdtempSync(join(tmpdir(), 'lock-'));
    dirs.push(d);
    return join(d, '.instance.lock');
  };

  afterEach(() => {
    for (const d of dirs.splice(0)) rmSync(d, { recursive: true, force: true });
  });

  it('acquires and releases', () => {
    const p = tmp();
    const lock = acquireLock(p);
    lock.release();
    // Re-acquire after release must succeed.
    acquireLock(p).release();
  });

  it('refuses a second live holder', () => {
    const p = tmp();
    const first = acquireLock(p);
    expect(() => acquireLock(p)).toThrow(LockError);
    first.release();
  });

  it('reclaims a stale lock from a dead pid', () => {
    const p = tmp();
    // PID 2^31-1 is effectively guaranteed not to exist.
    writeFileSync(p, '2147483646');
    const lock = acquireLock(p);
    expect(lock).toBeDefined();
    lock.release();
  });
});
