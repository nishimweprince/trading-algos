import pino from 'pino';
import type { AppConfig } from './config.js';

export function createLogger(config: AppConfig) {
  const transport =
    process.env.NODE_ENV !== 'production'
      ? {
          target: 'pino-pretty',
          options: { colorize: true, translateTime: 'SYS:standard' },
        }
      : undefined;

  return pino({
    level: config.LOG_LEVEL,
    ...(transport ? { transport } : {}),
  });
}

export type AppLogger = ReturnType<typeof createLogger>;
