import fs from 'node:fs/promises';
import path from 'node:path';
import input from 'input';
import { TelegramClient, sessions } from 'telegram';
import { config as loadEnv } from 'dotenv';
import { loadConfig } from '../src/config.js';

loadEnv();

async function chmod600(p: string): Promise<void> {
  try {
    await fs.chmod(p, 0o600);
  } catch {
    /* ignore */
  }
}

async function main(): Promise<void> {
  const cwd = process.cwd();
  let cfg;
  try {
    cfg = loadConfig(cwd);
  } catch (e) {
    console.error(e instanceof Error ? e.message : e);
    process.exit(1);
    return;
  }

  const phone = cfg.TELEGRAM_PHONE_NUMBER;
  if (!phone) {
    console.error('TELEGRAM_PHONE_NUMBER is required for login');
    process.exit(1);
    return;
  }

  const sessionPath = cfg.resolvedSessionPath;
  await fs.mkdir(path.dirname(sessionPath), { recursive: true });

  const client = new TelegramClient(new sessions.StringSession(''), cfg.TELEGRAM_API_ID, cfg.TELEGRAM_API_HASH, {
    connectionRetries: 5,
  });

  await client.start({
    phoneNumber: async () => phone,
    password: async () => await input.text('2FA password (if any): '),
    phoneCode: async () => await input.text('Telegram login code: '),
    onError: (err) => console.error(err),
  });

  const str = client.session.save() as unknown as string;
  await fs.writeFile(sessionPath, `${str}\n`, 'utf8');
  await chmod600(sessionPath);
  console.log(`Session saved to ${sessionPath} (chmod 600). You can start the service.`);
  await client.disconnect();
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
