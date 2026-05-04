import { describe, it, expect, afterEach } from 'vitest';
import fs from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import { readCursors, writeCursorsAtomic, type CursorsFile } from '../src/state/cursorStore.js';

describe('cursorStore', () => {
  let dir = '';
  let filePath = '';

  afterEach(async () => {
    if (dir) await fs.rm(dir, { recursive: true, force: true });
  });

  it('atomic write: reader sees old or new, not partial JSON', async () => {
    dir = await fs.mkdtemp(path.join(os.tmpdir(), 'cur-'));
    filePath = path.join(dir, 'cursors.json');
    const a: CursorsFile = {
      '@x': { channelId: '1', lastSeenMessageId: 1, updatedAt: 't' },
    };
    await writeCursorsAtomic(filePath, a);
    const b: CursorsFile = {
      '@x': { channelId: '1', lastSeenMessageId: 999, updatedAt: 't2' },
      '@y': { channelId: '2', lastSeenMessageId: 2, updatedAt: 't3' },
    };
    await writeCursorsAtomic(filePath, b);
    const read = await readCursors(filePath);
    expect(read['@x']?.lastSeenMessageId).toBe(999);
    expect(read['@y']).toBeDefined();
  });
});
