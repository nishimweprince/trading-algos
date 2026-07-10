import pino from 'pino';

export function createLogger(level: string, appName: string) {
  return pino({
    level,
    name: appName,
    redact: {
      paths: ['req.headers.authorization', 'req.headers.x-internal-api-key', 'Authorization', 'OANDA_API_TOKEN', 'INTERNAL_API_KEY', '*.password', '*.token'],
      censor: '[REDACTED]',
    },
  });
}

export type AppLogger = ReturnType<typeof createLogger>;
