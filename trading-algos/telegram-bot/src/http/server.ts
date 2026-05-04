import express from 'express';
import type { Server } from 'node:http';
import type { AppConfig } from '../config.js';
import type { AppLogger } from '../logger.js';
import { healthHandler, statusHandler, type StatusSnapshot } from './routes/health.js';

export function createApp(_config: AppConfig, logger: AppLogger, statusSnapshot: StatusSnapshot) {
  const app = express();
  app.disable('x-powered-by');
  app.get('/health', healthHandler);
  app.get('/status', statusHandler(statusSnapshot));
  app.use((err: unknown, _req: express.Request, res: express.Response, _next: express.NextFunction) => {
    logger.error({ err }, 'http error');
    res.status(500).json({ error: 'internal_error' });
  });
  return app;
}

export async function listenApp(
  app: ReturnType<typeof createApp>,
  host: string,
  port: number,
): Promise<{ server: Server; close: () => Promise<void> }> {
  const server = await new Promise<Server>((resolve, reject) => {
    const s = app.listen(port, host);
    s.once('listening', () => resolve(s));
    s.once('error', reject);
  });
  return {
    server,
    close: () =>
      new Promise((resolve, reject) => {
        server.close((e) => (e ? reject(e) : resolve(undefined)));
      }),
  };
}
