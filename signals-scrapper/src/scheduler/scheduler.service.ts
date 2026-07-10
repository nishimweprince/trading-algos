import { Injectable, Logger, OnModuleInit } from '@nestjs/common';
import { SchedulerRegistry } from '@nestjs/schedule';
import { CronJob } from 'cron';
import { AppConfigService } from '../config/app-config.service';
import { ScraperService } from '../scraper/scraper.service';

@Injectable()
export class SchedulerService implements OnModuleInit {
  private readonly logger = new Logger(SchedulerService.name);
  private isRunning = false;
  private job: CronJob | null = null;

  constructor(
    private readonly config: AppConfigService,
    private readonly scraper: ScraperService,
    private readonly schedulerRegistry: SchedulerRegistry,
  ) {}

  onModuleInit(): void {
    const expression = this.config.cronExpression;
    this.logger.log(`Registering scrape cron: ${expression}`);
    this.job = new CronJob(expression, () => {
      void this.handleCron();
    });
    this.schedulerRegistry.addCronJob('scrapeAllSources', this.job);
    this.job.start();
    this.logger.log('Cron job scrapeAllSources started');
  }

  /**
   * Cron handler with overlap guard. Also callable directly for tests.
   */
  async handleCron(): Promise<void> {
    if (this.isRunning) {
      this.logger.warn('Previous run still in progress; skipping this tick');
      return;
    }
    this.isRunning = true;
    try {
      this.logger.log('Cron tick: running all sources');
      await this.scraper.runAllSources();
    } catch (err) {
      this.logger.error(
        `Cron run failed: ${err instanceof Error ? err.message : String(err)}`,
      );
    } finally {
      this.isRunning = false;
    }
  }

  /** Test helper: is the overlap guard currently set? */
  get running(): boolean {
    return this.isRunning;
  }

  /** Test helper: force the running flag (e.g. simulate long run). */
  setRunning(value: boolean): void {
    this.isRunning = value;
  }

  getCronExpression(): string {
    return this.config.cronExpression;
  }

  isJobRegistered(): boolean {
    try {
      return !!this.schedulerRegistry.getCronJob('scrapeAllSources');
    } catch {
      return false;
    }
  }
}
