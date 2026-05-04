import type { TelegramClient } from 'telegram';
import type { CustomMessage } from 'telegram/tl/custom/message';

function messageDateIso(m: CustomMessage): string {
  const d = m.date;
  if (d instanceof Date) return d.toISOString();
  if (typeof d === 'number') return new Date(d * 1000).toISOString();
  return new Date().toISOString();
}

export function sortMessagesAsc(messages: CustomMessage[]): CustomMessage[] {
  return [...messages].sort((a, b) => {
    const da = a.date instanceof Date ? a.date.getTime() : Number(a.date) * 1000;
    const db = b.date instanceof Date ? b.date.getTime() : Number(b.date) * 1000;
    if (da !== db) return da - db;
    return Number(a.id) - Number(b.id);
  });
}

export async function getLatestMessage(
  client: TelegramClient,
  peer: Parameters<TelegramClient['getMessages']>[0],
): Promise<CustomMessage | undefined> {
  const res = await client.getMessages(peer, { limit: 1 });
  if (!res || res.length === 0) return undefined;
  return res[0] as CustomMessage;
}

export async function getMessagesSince(
  client: TelegramClient,
  peer: Parameters<TelegramClient['getMessages']>[0],
  minId: number,
  limit: number,
): Promise<CustomMessage[]> {
  const res = await client.getMessages(peer, { minId, limit });
  const arr = (res ?? []) as CustomMessage[];
  return sortMessagesAsc(arr);
}

export function getMessageText(m: CustomMessage): string {
  try {
    const t = m.text;
    return typeof t === 'string' ? t : '';
  } catch {
    return '';
  }
}

export { messageDateIso };
