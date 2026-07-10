import { z } from 'zod';
import { ApplicationError } from '../common/errors/application-error.js';
import { resolveOandaEnvironment } from './oanda-environment.js';

const booleanSchema = z.enum(['true', 'false']).transform((value) => value === 'true');
const positiveDecimalString = z.string().refine((value) => Number(value) > 0, 'must be positive');
const positiveIntegerString = z.string().refine((value) => Number.isInteger(Number(value)) && Number(value) > 0, 'must be a positive integer');

const envSchema = z.object({
  NODE_ENV: z.string().default('development'),
  PORT: positiveIntegerString.default('3000').transform(Number),
  HOST: z.string().default('0.0.0.0'),
  LOG_LEVEL: z.string().default('info'),
  APP_NAME: z.string().default('oanda-trading-server'),
  INTERNAL_API_KEY: z.string().min(16),
  OANDA_ENV: z.enum(['practice', 'live']).default('practice'),
  OANDA_ACCOUNT_ID: z.string().min(1),
  OANDA_API_TOKEN: z.string().min(1),
  LIVE_TRADING_ENABLED: booleanSchema.default(false),
  OANDA_ACCOUNT_ALIAS: z.string().default('primary'),
  DATABASE_URL: z.string().min(1),
  REDIS_URL: z.string().min(1),
  TRADING_ENABLED: booleanSchema.default(true),
  GLOBAL_KILL_SWITCH: booleanSchema.default(false),
  MAX_RISK_PER_TRADE_PERCENT: positiveDecimalString.default('0.50'),
  MAX_DAILY_LOSS_PERCENT: positiveDecimalString.default('2.00'),
  MAX_TOTAL_OPEN_RISK_PERCENT: positiveDecimalString.default('2.00'),
  MAX_OPEN_TRADES: positiveIntegerString.default('5').transform(Number),
  MAX_OPEN_TRADES_PER_INSTRUMENT: positiveIntegerString.default('1').transform(Number),
  MAX_SPREAD_PIPS: positiveDecimalString.default('3'),
  MAX_SIGNAL_AGE_SECONDS: positiveIntegerString.default('30').transform(Number),
  MAX_PRICE_AGE_SECONDS: positiveIntegerString.default('5').transform(Number),
  MAX_ORDER_UNITS: positiveIntegerString.default('100000').transform(Number),
  MIN_STOP_DISTANCE_PIPS: positiveDecimalString.default('5'),
  DEFAULT_RISK_REWARD_RATIO: positiveDecimalString.default('2'),
  ORDER_TIMEOUT_MS: positiveIntegerString.default('10000').transform(Number),
  REST_REQUEST_TIMEOUT_MS: positiveIntegerString.default('10000').transform(Number),
  MAX_RETRY_ATTEMPTS: positiveIntegerString.default('3').transform(Number),
  RETRY_BASE_DELAY_MS: positiveIntegerString.default('500').transform(Number),
  ACCOUNT_SYNC_INTERVAL_SECONDS: positiveIntegerString.default('15').transform(Number),
  TRANSACTION_SYNC_INTERVAL_SECONDS: positiveIntegerString.default('5').transform(Number),
  TELEGRAM_BOT_TOKEN: z.string().optional(),
  TELEGRAM_CHAT_ID: z.string().optional(),
  SLACK_WEBHOOK_URL: z.string().optional(),
}).superRefine((value, context) => {
  if (value.OANDA_ENV === 'live' && !value.LIVE_TRADING_ENABLED) {
    context.addIssue({ code: 'custom', path: ['LIVE_TRADING_ENABLED'], message: 'must be true when OANDA_ENV=live' });
  }
});

export type AppConfig = ReturnType<typeof loadConfig>;

export function loadConfig(source: NodeJS.ProcessEnv = process.env) {
  const parsed = envSchema.safeParse(source);
  if (!parsed.success) {
    const issues = parsed.error.issues.map((issue) => ({ path: issue.path.join('.'), message: issue.message }));
    throw new ApplicationError('CONFIGURATION_INVALID', 'Environment configuration is invalid.', 500, { issues });
  }
  const oandaEnvironment = resolveOandaEnvironment(parsed.data.OANDA_ENV);
  return { ...parsed.data, oandaEnvironment };
}

export function sanitizeConfig(config: AppConfig): Record<string, unknown> {
  return {
    nodeEnv: config.NODE_ENV,
    port: config.PORT,
    host: config.HOST,
    appName: config.APP_NAME,
    oandaEnv: config.OANDA_ENV,
    accountAlias: config.OANDA_ACCOUNT_ALIAS,
    tradingEnabled: config.TRADING_ENABLED,
    globalKillSwitch: config.GLOBAL_KILL_SWITCH,
  };
}
