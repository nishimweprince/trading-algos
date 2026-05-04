import { config as loadEnv } from 'dotenv';
import { loadConfig } from '../src/config.js';
import { readSessionString, createTelegramClient } from '../src/telegram/client.js';
import { resolveChannel } from '../src/telegram/channelResolver.js';

loadEnv();

async function main(): Promise<void> {
  const cwd = process.cwd();
  const cfg = loadConfig(cwd);
  const session = await readSessionString(cfg.resolvedSessionPath);
  if (!session) {
    console.error('No session file. Run npm run login first.');
    process.exit(1);
    return;
  }
  const client = createTelegramClient(session, cfg.TELEGRAM_API_ID, cfg.TELEGRAM_API_HASH);
  await client.connect();
  if (!(await client.checkAuthorization())) {
    console.error('Session not authorized.');
    process.exit(1);
    return;
  }

  const dialogs = await client.getDialogs({ limit: 50 });
  console.log('Recent dialogs (channels/chats):');
  for (const d of dialogs) {
    const title = d.title ?? d.name ?? '';
    console.log(`- ${title}`);
  }

  console.log('\nResolving configured channels:');
  for (const ch of cfg.TELEGRAM_CHANNELS) {
    try {
      const r = await resolveChannel(client, ch);
      console.log(`${ch} -> peerId ${r.channelId}`);
    } catch (e) {
      console.error(`${ch} FAILED:`, e instanceof Error ? e.message : e);
    }
  }

  await client.disconnect();
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
