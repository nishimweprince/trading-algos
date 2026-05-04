import fs from 'node:fs/promises';
import path from 'node:path';

export type ChannelCursor = {
  channelId: string;
  lastSeenMessageId: number;
  updatedAt: string;
};

export type CursorsFile = Record<string, ChannelCursor>;

export async function readCursors(filePath: string): Promise<CursorsFile> {
  try {
    const raw = await fs.readFile(filePath, 'utf8');
    const parsed = JSON.parse(raw) as CursorsFile;
    return parsed && typeof parsed === 'object' ? parsed : {};
  } catch (e) {
    const err = e as NodeJS.ErrnoException;
    if (err.code === 'ENOENT') return {};
    throw e;
  }
}

export async function writeCursorsAtomic(filePath: string, data: CursorsFile): Promise<void> {
  const dir = path.dirname(filePath);
  await fs.mkdir(dir, { recursive: true });
  const tmp = `${filePath}.${process.pid}.${Date.now()}.tmp`;
  const json = `${JSON.stringify(data, null, 2)}\n`;
  await fs.writeFile(tmp, json, 'utf8');
  await fs.rename(tmp, filePath);
}
