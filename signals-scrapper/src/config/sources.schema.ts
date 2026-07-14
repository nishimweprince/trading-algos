import { z } from 'zod';
import { existsSync, statSync } from 'fs';
import { isAbsolute } from 'path';

export const ProviderTypeSchema = z.enum(['TRADING_CENTRAL', 'AUTOCHARTIST']);

export const SourceConfigSchema = z.object({
  type: ProviderTypeSchema,
  url: z.string().url(),
});

export const SourcesSchema = z.array(SourceConfigSchema).min(1);

export type SourceConfig = z.infer<typeof SourceConfigSchema>;
export type SourcesConfig = z.infer<typeof SourcesSchema>;

export const BrowserModeSchema = z.enum(['CDP', 'PERSISTENT']);
export const HostOsSchema = z.enum(['AUTO', 'MACOS', 'WINDOWS']);
export type HostOs = z.infer<typeof HostOsSchema>;
export type ResolvedHostOs = Exclude<HostOs, 'AUTO'>;

const EnvBooleanSchema = z
  .union([z.boolean(), z.enum(['true', 'false', '1', '0'])])
  .transform((v) => {
    if (typeof v === 'boolean') return v;
    return v.toLowerCase() === 'true' || v === '1';
  });

const PositiveDecimalStringSchema = z
  .string()
  .regex(/^\d+(?:\.\d+)?$/, 'volume must be a positive decimal string')
  .refine((value) => Number(value) > 0, 'volume must be greater than zero');

export const Mt5SignalRuleSchema = z.object({
  symbol: z.string().trim().min(1).max(64),
  volume: PositiveDecimalStringSchema,
});

export const Mt5SignalRulesSchema = z.record(
  z.string().trim().min(1),
  Mt5SignalRuleSchema,
);
export type Mt5SignalRules = z.infer<typeof Mt5SignalRulesSchema>;

export const AppConfigSchema = z.object({
  SOURCES: SourcesSchema,
  BROWSER_MODE: BrowserModeSchema.default('PERSISTENT'),
  CDP_ENDPOINT: z.string().url().default('http://127.0.0.1:9222'),
  CDP_AUTO_START: EnvBooleanSchema.default(true),
  CDP_STARTUP_TIMEOUT_MS: z.coerce.number().int().positive().default(20000),
  HOST_OS: HostOsSchema.default('AUTO'),
  CHROME_EXECUTABLE_PATH: z.string().default(''),
  USER_DATA_DIR: z.string().default('./.chrome-profile'),
  CRON_EXPRESSION: z.string().default('*/15 * * * *'),
  IDEAS_LOG_PATH: z.string().default('./data/ideas.jsonl'),
  SCREENSHOT_DIR: z.string().default('./data/screenshots'),
  SEEN_STATE_PATH: z.string().default('./data/seen.json'),
  DEBUG_RUN_MAX_ENTRIES: z.coerce.number().int().positive().default(100),
  OLLAMA_API_KEY: z.string().default(''),
  OLLAMA_MODEL: z.string().min(1).default('gemma4:31b'),
  OLLAMA_HOST: z.string().url().default('https://ollama.com'),
  OLLAMA_TIMEOUT_MS: z.coerce.number().int().positive().default(60000),
  MT5_SIGNAL_TRADING_ENABLED: EnvBooleanSchema.default(false),
  MT5_SIGNAL_API_URL: z.string().url().default('http://127.0.0.1:8000'),
  MT5_SIGNAL_API_KEY: z.string().default(''),
  MT5_SIGNAL_TIMEOUT_MS: z.coerce.number().int().positive().default(70000),
  MT5_EXECUTION_MAX_ENTRIES: z.coerce.number().int().positive().default(5000),
  MT5_SIGNAL_RULES: Mt5SignalRulesSchema.default({}),
  HEADLESS: EnvBooleanSchema.default(false),
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
  const mt5SignalRules = parseMt5SignalRules(env.MT5_SIGNAL_RULES);
  const raw = {
    SOURCES: sources,
    BROWSER_MODE: env.BROWSER_MODE ?? 'PERSISTENT',
    CDP_ENDPOINT: env.CDP_ENDPOINT ?? 'http://127.0.0.1:9222',
    CDP_AUTO_START: env.CDP_AUTO_START ?? 'true',
    CDP_STARTUP_TIMEOUT_MS: env.CDP_STARTUP_TIMEOUT_MS ?? '20000',
    HOST_OS: env.HOST_OS ?? 'AUTO',
    CHROME_EXECUTABLE_PATH: env.CHROME_EXECUTABLE_PATH ?? '',
    USER_DATA_DIR: env.USER_DATA_DIR ?? './.chrome-profile',
    CRON_EXPRESSION: env.CRON_EXPRESSION ?? '*/15 * * * *',
    IDEAS_LOG_PATH: env.IDEAS_LOG_PATH ?? './data/ideas.jsonl',
    SCREENSHOT_DIR: env.SCREENSHOT_DIR ?? './data/screenshots',
    SEEN_STATE_PATH: env.SEEN_STATE_PATH ?? './data/seen.json',
    DEBUG_RUN_MAX_ENTRIES: env.DEBUG_RUN_MAX_ENTRIES ?? '100',
    OLLAMA_API_KEY: env.OLLAMA_API_KEY ?? '',
    OLLAMA_MODEL: env.OLLAMA_MODEL ?? 'gemma4:31b',
    OLLAMA_HOST: env.OLLAMA_HOST ?? 'https://ollama.com',
    OLLAMA_TIMEOUT_MS: env.OLLAMA_TIMEOUT_MS ?? '60000',
    MT5_SIGNAL_TRADING_ENABLED:
      env.MT5_SIGNAL_TRADING_ENABLED ?? 'false',
    MT5_SIGNAL_API_URL:
      env.MT5_SIGNAL_API_URL ?? 'http://127.0.0.1:8000',
    MT5_SIGNAL_API_KEY: env.MT5_SIGNAL_API_KEY ?? '',
    MT5_SIGNAL_TIMEOUT_MS: env.MT5_SIGNAL_TIMEOUT_MS ?? '70000',
    MT5_EXECUTION_MAX_ENTRIES: env.MT5_EXECUTION_MAX_ENTRIES ?? '5000',
    MT5_SIGNAL_RULES: mt5SignalRules,
    HEADLESS: env.HEADLESS ?? 'false',
    NAV_TIMEOUT_MS: env.NAV_TIMEOUT_MS ?? '30000',
    CONTENT_WAIT_MS: env.CONTENT_WAIT_MS ?? '10000',
    SEEN_MAX_ENTRIES: env.SEEN_MAX_ENTRIES ?? '5000',
  };
  return AppConfigSchema.parse(raw);
}

/** Runtime-only requirements for the screenshot/Ollama Trading Central path. */
export function assertRuntimeConfig(config: AppConfig): void {
  const hasTradingCentral = config.SOURCES.some(
    (source) => source.type === 'TRADING_CENTRAL',
  );
  if (hasTradingCentral && config.BROWSER_MODE !== 'CDP') {
    throw new Error(
      'TRADING_CENTRAL requires BROWSER_MODE=CDP so the bot can reuse an already-open authenticated Chrome tab.',
    );
  }
  if (hasTradingCentral && !config.OLLAMA_API_KEY.trim()) {
    throw new Error(
      'OLLAMA_API_KEY is required when a TRADING_CENTRAL source is configured.',
    );
  }
  if (config.CHROME_EXECUTABLE_PATH.trim()) {
    if (!isAbsolute(config.CHROME_EXECUTABLE_PATH)) {
      throw new Error('CHROME_EXECUTABLE_PATH must be an absolute path.');
    }
    if (
      !existsSync(config.CHROME_EXECUTABLE_PATH) ||
      !statSync(config.CHROME_EXECUTABLE_PATH).isFile()
    ) {
      throw new Error('CHROME_EXECUTABLE_PATH does not point to an existing file.');
    }
  }
  if (
    config.BROWSER_MODE === 'CDP' &&
    (config.HOST_OS !== 'AUTO' ||
      (config.CDP_AUTO_START && isLoopbackCdpEndpoint(config.CDP_ENDPOINT)))
  ) {
    resolveHostOs(config.HOST_OS);
  }
  if (config.MT5_SIGNAL_TRADING_ENABLED) {
    if (config.MT5_SIGNAL_API_KEY.trim().length < 16) {
      throw new Error(
        'MT5_SIGNAL_API_KEY must contain at least 16 characters when MT5 signal trading is enabled.',
      );
    }
    if (Object.keys(config.MT5_SIGNAL_RULES).length === 0) {
      throw new Error(
        'MT5_SIGNAL_RULES must contain at least one symbol and volume rule when MT5 signal trading is enabled.',
      );
    }
  }
}

export function isLoopbackCdpEndpoint(endpoint: string): boolean {
  const hostname = new URL(endpoint).hostname.toLowerCase().replace(/^\[|\]$/g, '');
  return hostname === '127.0.0.1' || hostname === 'localhost' || hostname === '::1';
}

export function detectHostOs(
  platform: NodeJS.Platform = process.platform,
): ResolvedHostOs | undefined {
  if (platform === 'darwin') return 'MACOS';
  if (platform === 'win32') return 'WINDOWS';
  return undefined;
}

export function resolveHostOs(
  configured: HostOs,
  platform: NodeJS.Platform = process.platform,
): ResolvedHostOs {
  const detected = detectHostOs(platform);
  if (!detected) {
    throw new Error(
      `Chrome auto-start is unsupported on platform ${platform}; set CDP_AUTO_START=false and start Chrome manually.`,
    );
  }
  if (configured !== 'AUTO' && configured !== detected) {
    throw new Error(
      `HOST_OS=${configured} does not match the detected platform ${detected}.`,
    );
  }
  return configured === 'AUTO' ? detected : configured;
}

/** Parse the JSON rules map without ever including secret configuration in errors. */
export function parseMt5SignalRules(raw: string | undefined): Mt5SignalRules {
  if (raw === undefined || raw.trim() === '') return {};
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    throw new Error(`MT5_SIGNAL_RULES is not valid JSON: ${message}`);
  }
  return Mt5SignalRulesSchema.parse(parsed);
}
