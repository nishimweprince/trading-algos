import { z } from 'zod';

export const ProviderTypeSchema = z.enum(['TRADING_CENTRAL', 'AUTOCHARTIST']);

export const SourceConfigSchema = z.object({
  type: ProviderTypeSchema,
  url: z.string().url(),
});

export const SourcesSchema = z.array(SourceConfigSchema).min(1);

export type SourceConfig = z.infer<typeof SourceConfigSchema>;
export type SourcesConfig = z.infer<typeof SourcesSchema>;

export const BrowserModeSchema = z.enum(['CDP', 'PERSISTENT']);

export const AppConfigSchema = z.object({
  SOURCES: SourcesSchema,
  BROWSER_MODE: BrowserModeSchema.default('PERSISTENT'),
  CDP_ENDPOINT: z.string().default('http://127.0.0.1:9222'),
  USER_DATA_DIR: z.string().default('./.chrome-profile'),
  CRON_EXPRESSION: z.string().default('*/15 * * * *'),
  IDEAS_LOG_PATH: z.string().default('./data/ideas.jsonl'),
  SCREENSHOT_DIR: z.string().default('./data/screenshots'),
  SEEN_STATE_PATH: z.string().default('./data/seen.json'),
  HEADLESS: z
    .union([z.boolean(), z.string()])
    .transform((v) => {
      if (typeof v === 'boolean') return v;
      return v.toLowerCase() === 'true' || v === '1';
    })
    .default(false),
  NAV_TIMEOUT_MS: z.coerce.number().int().positive().default(30000),
  /**
   * After navigation, wait this long before reading login wall / iframe content.
   * Trading Central loads ideas inside a Recognia iframe that needs settle time.
   */
  CONTENT_WAIT_MS: z.coerce.number().int().nonnegative().default(10000),
  SEEN_MAX_ENTRIES: z.coerce.number().int().positive().default(5000),
});

export type AppConfig = z.infer<typeof AppConfigSchema>;

/**
 * Parse and validate SOURCES env JSON string.
 * Throws ZodError (or SyntaxError) on invalid input — fail-fast at boot.
 */
export function parseSources(raw: string | undefined): SourcesConfig {
  if (raw === undefined || raw === null || String(raw).trim() === '') {
    throw new Error(
      'SOURCES environment variable is required. Expected a JSON array of { type, url }.',
    );
  }
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    throw new Error(`SOURCES is not valid JSON: ${msg}`);
  }
  return SourcesSchema.parse(parsed);
}

/**
 * Load and validate full app config from a process.env-like record.
 */
export function loadAppConfig(env: NodeJS.ProcessEnv = process.env): AppConfig {
  const sources = parseSources(env.SOURCES);
  const raw = {
    SOURCES: sources,
    BROWSER_MODE: env.BROWSER_MODE ?? 'PERSISTENT',
    CDP_ENDPOINT: env.CDP_ENDPOINT ?? 'http://127.0.0.1:9222',
    USER_DATA_DIR: env.USER_DATA_DIR ?? './.chrome-profile',
    CRON_EXPRESSION: env.CRON_EXPRESSION ?? '*/15 * * * *',
    IDEAS_LOG_PATH: env.IDEAS_LOG_PATH ?? './data/ideas.jsonl',
    SCREENSHOT_DIR: env.SCREENSHOT_DIR ?? './data/screenshots',
    SEEN_STATE_PATH: env.SEEN_STATE_PATH ?? './data/seen.json',
    HEADLESS: env.HEADLESS ?? 'false',
    NAV_TIMEOUT_MS: env.NAV_TIMEOUT_MS ?? '30000',
    CONTENT_WAIT_MS: env.CONTENT_WAIT_MS ?? '10000',
    SEEN_MAX_ENTRIES: env.SEEN_MAX_ENTRIES ?? '5000',
  };
  return AppConfigSchema.parse(raw);
}
