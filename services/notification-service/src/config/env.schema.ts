import { z } from 'zod';

export const NotificationChannelEnum = z.enum([
  'TELEGRAM',
  'EMAIL',
  'SMS',
  'WHATSAPP',
]);

export type NotificationChannel = z.infer<typeof NotificationChannelEnum>;

function parseCommaList(value: string | undefined): string[] {
  if (!value?.trim()) return [];
  return value
    .split(',')
    .map((s) => s.trim())
    .filter(Boolean);
}

function parseBool(value: string | undefined, defaultValue: boolean): boolean {
  if (value === undefined || value === '') return defaultValue;
  return value.toLowerCase() === 'true' || value === '1';
}

export const envSchema = z.object({
  PORT: z.coerce.number().int().positive().default(3010),
  DATABASE_URL: z.string().default('file:./data/notifications.db'),
  NOTIFICATIONS_ENABLED: z
    .string()
    .optional()
    .transform((v) => parseBool(v, true)),
  NOTIFICATION_API_KEY: z.string().optional(),
  NOTIFICATION_PHONES: z.string().optional(),
  NOTIFICATION_EMAILS: z.string().optional(),
  NOTIFICATION_TELEGRAM_CHAT_IDS: z.string().optional(),
  TG_BOT_TOKEN: z.string().optional(),
  RESEND_API_KEY: z.string().optional(),
  NOTIFICATION_FROM_EMAIL: z.string().optional(),
  PINDO_API_URL: z.string().default('https://api.pindo.io/v1/sms/'),
  PINDO_TOKEN: z.string().optional(),
  PINDO_SENDER_ID: z.string().default('Notifications'),
  WHATSAPP_ACCESS_TOKEN: z.string().optional(),
  WHATSAPP_PHONE_NUMBER_ID: z.string().optional(),
  WHATSAPP_API_VERSION: z.string().default('v21.0'),
  WHATSAPP_VERIFY_TOKEN: z.string().optional(),
  WHATSAPP_APP_SECRET: z.string().optional(),
});

export type EnvConfig = z.infer<typeof envSchema>;

export type ParsedConfig = EnvConfig & {
  notificationPhones: string[];
  notificationEmails: string[];
  notificationTelegramChatIds: string[];
};

export function loadEnvConfig(env: NodeJS.ProcessEnv = process.env): ParsedConfig {
  const parsed = envSchema.parse(env);
  return {
    ...parsed,
    notificationPhones: parseCommaList(parsed.NOTIFICATION_PHONES),
    notificationEmails: parseCommaList(parsed.NOTIFICATION_EMAILS),
    notificationTelegramChatIds: parseCommaList(
      parsed.NOTIFICATION_TELEGRAM_CHAT_IDS,
    ),
  };
}
