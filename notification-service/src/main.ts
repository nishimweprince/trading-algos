import { Logger, ValidationPipe } from '@nestjs/common';
import { NestFactory } from '@nestjs/core';
import { NestExpressApplication } from '@nestjs/platform-express';
import { mkdirSync } from 'fs';
import { dirname, resolve } from 'path';
import { json } from 'express';
import { AppModule } from './app.module';
import { AppConfigService } from './config/config.service';
import { loadEnvConfig } from './config/env.schema';

async function bootstrap(): Promise<void> {
  const logger = new Logger('Bootstrap');

  try {
    loadEnvConfig(process.env);
    logger.log('Environment configuration validated');
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    logger.error(`Invalid configuration: ${msg}`);
    process.exit(1);
  }

  const app = await NestFactory.create<NestExpressApplication>(AppModule, {
    logger: ['error', 'warn', 'log'],
    bodyParser: false,
  });

  app.use(
    json({
      verify: (req, _res, buf) => {
        (req as { rawBody?: Buffer }).rawBody = buf;
      },
    }),
  );

  app.useGlobalPipes(
    new ValidationPipe({
      whitelist: true,
      transform: true,
      forbidNonWhitelisted: true,
    }),
  );

  app.enableShutdownHooks();

  const config = app.get(AppConfigService);
  const dbDir = dirname(resolve(process.cwd(), config.databasePath));
  mkdirSync(dbDir, { recursive: true });

  const port = config.port;
  await app.listen(port);
  logger.log(`Notification service listening on port ${port}`);
}

bootstrap().catch((err) => {
  const logger = new Logger('Bootstrap');
  logger.error(
    `Fatal bootstrap error: ${err instanceof Error ? err.message : String(err)}`,
  );
  process.exit(1);
});
