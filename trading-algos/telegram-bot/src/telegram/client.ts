import fs from 'node:fs/promises';
import { TelegramClient, sessions } from 'telegram';

export async function readSessionString(sessionPath: string): Promise<string | null> {
  try {
    const s = (await fs.readFile(sessionPath, 'utf8')).trim();
    return s.length > 0 ? s : null;
  } catch (e) {
    const err = e as NodeJS.ErrnoException;
    if (err.code === 'ENOENT') return null;
    throw e;
  }
}

export function createTelegramClient(
  sessionString: string,
  apiId: number,
  apiHash: string,
): TelegramClient {
  return new TelegramClient(new sessions.StringSession(sessionString), apiId, apiHash, {
    connectionRetries: 5,
  });
}
