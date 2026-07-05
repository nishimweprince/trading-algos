import { openSync, closeSync, writeSync, readFileSync, unlinkSync, existsSync } from 'node:fs';
import { resolve } from 'node:path';

/**
 * Single-instance lock (Section 2/12). Two instances trading the same wallet
 * would double-fill and blow through size limits, so startup acquires an
 * exclusive lock file. Acquisition is atomic via O_EXCL; a lock left behind by
 * a dead process is detected (PID no longer alive) and reclaimed.
 */

export class LockError extends Error {
  override name = 'LockError';
}

function pidAlive(pid: number): boolean {
  try {
    // Signal 0 performs error checking without sending a signal.
    process.kill(pid, 0);
    return true;
  } catch (err) {
    // ESRCH = no such process. EPERM = exists but not ours — still alive.
    return (err as NodeJS.ErrnoException).code === 'EPERM';
  }
}

export interface InstanceLock {
  release(): void;
}

export function acquireLock(lockPath = '.instance.lock'): InstanceLock {
  const path = resolve(lockPath);

  const tryCreate = (): number | null => {
    try {
      // wx = create for writing, fail if it already exists (atomic).
      return openSync(path, 'wx');
    } catch (err) {
      if ((err as NodeJS.ErrnoException).code === 'EEXIST') return null;
      throw err;
    }
  };

  let fd = tryCreate();

  if (fd === null) {
    // Lock exists — is the owner still alive?
    let ownerPid = NaN;
    try {
      ownerPid = Number.parseInt(readFileSync(path, 'utf8').trim(), 10);
    } catch {
      /* unreadable — treat as stale below */
    }

    if (Number.isFinite(ownerPid) && pidAlive(ownerPid)) {
      throw new LockError(
        `another instance is already running (pid ${ownerPid}, lock ${path}). ` +
          `Refusing to start — two instances would double-trade the wallet.`,
      );
    }

    // Stale lock from a dead process — reclaim it.
    if (existsSync(path)) unlinkSync(path);
    fd = tryCreate();
    if (fd === null) {
      throw new LockError(`lost a race reclaiming stale lock at ${path}; retry`);
    }
  }

  writeSync(fd, String(process.pid));
  closeSync(fd);

  let released = false;
  const release = () => {
    if (released) return;
    released = true;
    try {
      if (existsSync(path)) unlinkSync(path);
    } catch {
      /* best effort */
    }
  };

  return { release };
}
