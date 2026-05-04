import { config as loadEnv } from 'dotenv';
import path from 'node:path';
import { z } from 'zod';

loadEnv();

const boolFromEnv = z
  .string()
  .optional()
  .transform((v) => {
    if (v === undefined || v === '') return false;
    const s = v.toLowerCase().trim();
    return s === '1' || s === 'true' || s === 'yes';
  });

const boolFromEnvDefaultTrue = z
  .string()
  .optional()
  .transform((v) => {
    if (v === undefined || v === '') return true;
    const s = v.toLowerCase().trim();
    if (s === '0' || s === 'false' || s === 'no') return false;
    return s === '1' || s === 'true' || s === 'yes';
  });

const commaList = z
  .string()
  .optional()
  .transform((s) =>
    (s ?? '')
      .split(',')
      .map((x) => x.trim())
      .filter(Boolean),
  );

const channelsSchema = z
  .string()
  .min(1, 'TELEGRAM_CHANNELS is required')
  .transform((s) =>
    s
      .split(',')
      .map((x) => x.trim())
      .filter(Boolean)
      .map((c) => (c.startsWith('@') ? c : `@${c}`)),
  )
  .refine((arr) => arr.length > 0, 'TELEGRAM_CHANNELS must list at least one channel');

const envSchema = z.object({
  TELEGRAM_API_ID: z.coerce.number().int().positive(),
  TELEGRAM_API_HASH: z.string().min(1),
  TELEGRAM_PHONE_NUMBER: z.string().optional(),
  TELEGRAM_SESSION_PATH: z.string().default('./state/session.txt'),

  TELEGRAM_CHANNELS: channelsSchema,
  POLL_INTERVAL_SECONDS: z.coerce.number().int().min(1).default(60),
  POLL_MAX_MESSAGES_PER_CHANNEL: z.coerce.number().int().min(1).max(500).default(50),

  STATE_DIR: z.string().default('./state'),
  LOGS_DIR: z.string().default('./logs'),
  LOG_LEVEL: z.string().default('info'),
  LOG_RAW_MESSAGES: boolFromEnv,

  PINDO_API_URL: z.string().url().default('https://api.pindo.io/v1/sms/'),
  PINDO_TOKEN: z.string().optional(),
  PINDO_SENDER_ID: z.string().min(1).default('YourCompany'),

  NOTIFICATIONS_ENABLED: boolFromEnvDefaultTrue,
  NOTIFICATION_NUMBERS: commaList,

  HTTP_PORT: z.coerce.number().int().min(1).max(65535).default(8081),
  HTTP_HOST: z.string().default('127.0.0.1'),
});

export type AppConfig = z.infer<typeof envSchema> & {
  resolvedSessionPath: string;
  resolvedStateDir: string;
  resolvedLogsDir: string;
  resolvedCursorsPath: string;
  resolvedHandledPath: string;
};

function resolveFromRoot(p: string, cwd: string): string {
  return path.isAbsolute(p) ? p : path.resolve(cwd, p);
}

export function loadConfig(cwd = process.cwd()): AppConfig {
  const parsed = envSchema.safeParse(process.env);
  if (!parsed.success) {
    const msg = parsed.error.errors.map((e) => `${e.path.join('.')}: ${e.message}`).join('\n');
    throw new Error(`Invalid environment:\n${msg}`);
  }
  const e = parsed.data;
  const resolvedStateDir = resolveFromRoot(e.STATE_DIR, cwd);
  const resolvedLogsDir = resolveFromRoot(e.LOGS_DIR, cwd);
  const resolvedSessionPath = resolveFromRoot(e.TELEGRAM_SESSION_PATH, cwd);
  return {
    ...e,
    LOG_RAW_MESSAGES: e.LOG_RAW_MESSAGES ?? false,
    resolvedSessionPath,
    resolvedStateDir,
    resolvedLogsDir,
    resolvedCursorsPath: path.join(resolvedStateDir, 'cursors.json'),
    resolvedHandledPath: path.join(resolvedStateDir, 'handled.json'),
  };
}
