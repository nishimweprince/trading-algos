import { describe, it, expect } from 'vitest';
import fs from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import { Deduper } from '../src/signals/deduper.js';

describe('Deduper', () => {
  it('returns true on second check for same channel+message', () => {
    const d = new Deduper();
    expect(d.alreadyHandled('c1', 5)).toBe(false);
    d.markHandledMemory('c1', 5);
    expect(d.alreadyHandled('c1', 5)).toBe(true);
  });

  it('persists across load', async () => {
    const dir = await fs.mkdtemp(path.join(os.tmpdir(), 'dedup-'));
    const file = path.join(dir, 'handled.json');
    try {
      const d1 = new Deduper();
      d1.markHandledMemory('ch', 99);
      await Deduper.persistAtomic(file, d1.toFile());
      const d2 = await Deduper.load(file);
      expect(d2.alreadyHandled('ch', 99)).toBe(true);
    } finally {
      await fs.rm(dir, { recursive: true, force: true });
    }
  });
});
