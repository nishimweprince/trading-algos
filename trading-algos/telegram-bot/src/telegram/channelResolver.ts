import type { Api } from 'telegram';
import type { TelegramClient } from 'telegram';

export type ResolvedChannel = {
  username: string;
  entity: Api.User | Api.Chat | Api.Channel;
  channelId: string;
};

const cache = new Map<string, ResolvedChannel>();

export function clearChannelResolverCache(): void {
  cache.clear();
}

export async function resolveChannel(
  client: TelegramClient,
  username: string,
): Promise<ResolvedChannel> {
  const hit = cache.get(username);
  if (hit) return hit;
  const entity = (await client.getEntity(username)) as Api.User | Api.Chat | Api.Channel;
  const channelId = await client.getPeerId(entity);
  const resolved: ResolvedChannel = { username, entity, channelId };
  cache.set(username, resolved);
  return resolved;
}
