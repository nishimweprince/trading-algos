import { Logger } from '@nestjs/common';
import { NestFactory } from '@nestjs/core';
import { config as loadDotenv } from 'dotenv';
import { resolve } from 'path';
import { AppModule } from './app.module';
import { loadAppConfig } from './config/sources.schema';

// Load .env before fail-fast validation (Nest ConfigModule runs later).
loadDotenv({ path: resolve(process.cwd(), '.env') });

async function bootstrap(): Promise<void> {
  const logger = new Logger('Bootstrap');

  // Fail-fast on invalid SOURCES / config BEFORE Nest DI spins up scrapers.
  try {
    const cfg = loadAppConfig(process.env);
    logger.log(
      `Config OK: ${cfg.SOURCES.length} source(s), browser=${cfg.BROWSER_MODE}, cron=${cfg.CRON_EXPRESSION}`,
    );
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    // Zod errors include path details
    logger.error(`Invalid configuration: ${msg}`);
    if (err && typeof err === 'object' && 'issues' in err) {
      logger.error(JSON.stringify((err as { issues: unknown }).issues, null, 2));
    }
    process.exit(1);
  }

  const app = await NestFactory.createApplicationContext(AppModule, {
    logger: ['error', 'warn', 'log'],
  });
  app.enableShutdownHooks();

  logger.log('Trading Ideas Logger Bot is running (scheduled)');
}

bootstrap().catch((err) => {
  const logger = new Logger('Bootstrap');
  logger.error(
    `Fatal bootstrap error: ${err instanceof Error ? err.message : String(err)}`,
  );
  process.exit(1);
});
