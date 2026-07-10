import { Injectable, Logger, OnModuleDestroy } from '@nestjs/common';
import { BrowserService } from './browser/browser.service';
import { DedupService } from './dedup/dedup.service';

/**
 * Ensures graceful shutdown: flush seen state and detach/close browser.
 * Nest calls OnModuleDestroy on SIGTERM/SIGINT when enableShutdownHooks is set.
 */
@Injectable()
export class ShutdownService implements OnModuleDestroy {
  private readonly logger = new Logger(ShutdownService.name);

  constructor(
    private readonly dedup: DedupService,
    private readonly browser: BrowserService,
  ) {}

  async onModuleDestroy(): Promise<void> {
    this.logger.log('Graceful shutdown: flushing seen state and closing browser');
    try {
      this.dedup.flush();
    } catch (err) {
      this.logger.warn(
        `Seen flush failed: ${err instanceof Error ? err.message : String(err)}`,
      );
    }
    try {
      await this.browser.close();
    } catch (err) {
      this.logger.warn(
        `Browser close failed: ${err instanceof Error ? err.message : String(err)}`,
      );
    }
  }
}
