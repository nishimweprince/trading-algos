import type { AppLogger } from '../logger.js';

export async function* pollLoopTicks(
  intervalMs: number,
  startTime: number,
  shouldStop: () => boolean,
): AsyncGenerator<number> {
  let next = startTime + Math.ceil((Date.now() - startTime) / intervalMs) * intervalMs;
  while (!shouldStop()) {
    const now = Date.now();
    const wait = Math.max(0, next - now);
    if (wait > 0) {
      await new Promise<void>((resolve) => setTimeout(resolve, wait));
    }
    if (shouldStop()) break;
    yield next;
    next += intervalMs;
    const drift = Date.now() - next;
    if (drift > intervalMs) {
      next += Math.ceil(drift / intervalMs) * intervalMs;
    }
  }
}

export function createShutdownController(logger: AppLogger) {
  let stopping = false;
  const onSignal = () => {
    if (stopping) return;
    stopping = true;
    logger.info('shutdown signal received');
  };
  process.on('SIGINT', onSignal);
  process.on('SIGTERM', onSignal);
  return {
    shouldStop: () => stopping,
    dispose: () => {
      process.off('SIGINT', onSignal);
      process.off('SIGTERM', onSignal);
    },
  };
}
