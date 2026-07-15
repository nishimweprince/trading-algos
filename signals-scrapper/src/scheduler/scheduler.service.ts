import { Injectable, Logger, OnModuleInit } from '@nestjs/common';
import { SchedulerRegistry } from '@nestjs/schedule';
import { CronJob } from 'cron';
import { AppConfigService } from '../config/app-config.service';
import { ScraperService } from '../scraper/scraper.service';

@Injectable()
export class SchedulerService implements OnModuleInit {
  private readonly logger = new Logger(SchedulerService.name);
  private activeTask: 'signals' | 'auth-refresh' | null = null;
  private authRefreshCompletion: Promise<void> | null = null;
  private signalJob: CronJob | null = null;
  private authRefreshJob: CronJob | null = null;

  constructor(
    private readonly config: AppConfigService,
    private readonly scraper: ScraperService,
    private readonly schedulerRegistry: SchedulerRegistry,
  ) {}

  onModuleInit(): void {
    const signalExpression = this.config.signalCronExpression;
    this.logger.log(`Registering signal extraction cron: ${signalExpression}`);
    this.signalJob = new CronJob(signalExpression, () => {
      void this.handleSignalCron();
    });
    this.schedulerRegistry.addCronJob('extractSignals', this.signalJob);
    this.signalJob.start();

    const refreshExpression = this.config.authRefreshCronExpression;
    this.logger.log(`Registering authentication refresh cron: ${refreshExpression}`);
    this.authRefreshJob = new CronJob(refreshExpression, () => {
      void this.handleAuthRefreshCron();
    });
    this.schedulerRegistry.addCronJob(
      'refreshAuthenticatedPages',
      this.authRefreshJob,
    );
    this.authRefreshJob.start();
    this.logger.log('Signal extraction and authentication refresh cron jobs started');
  }

  /**
   * Signal cron handler with a shared browser overlap guard.
   */
  async handleSignalCron(): Promise<void> {
    if (this.activeTask === 'auth-refresh' && this.authRefreshCompletion) {
      this.logger.log(
        'Authentication refresh is finishing; delaying signal extraction tick',
      );
      await this.authRefreshCompletion;
    }
    if (this.activeTask) {
      this.logger.warn(
        `${this.activeTask} task still in progress; skipping signal extraction tick`,
      );
      return;
    }
    this.activeTask = 'signals';
    try {
      this.logger.log('Signal cron tick: running all sources');
      await this.scraper.runAllSources();
    } catch (err) {
      this.logger.error(
        `Signal cron run failed: ${err instanceof Error ? err.message : String(err)}`,
      );
    } finally {
      this.activeTask = null;
    }
  }

  /** Reload tabs only. No screenshot, OpenAI, persistence, or MT5 work. */
  async handleAuthRefreshCron(): Promise<void> {
    if (this.activeTask) {
      this.logger.warn(
        `${this.activeTask} task still in progress; skipping authentication refresh tick`,
      );
      return;
    }
    this.activeTask = 'auth-refresh';
    this.authRefreshCompletion = this.runAuthRefresh();
    await this.authRefreshCompletion;
  }

  private async runAuthRefresh(): Promise<void> {
    try {
      this.logger.log('Authentication refresh cron tick: reloading source tabs');
      await this.scraper.refreshAuthenticatedPages();
    } catch (err) {
      this.logger.error(
        `Authentication refresh failed: ${err instanceof Error ? err.message : String(err)}`,
      );
    } finally {
      this.activeTask = null;
      this.authRefreshCompletion = null;
    }
  }

  /** Backward-compatible alias for callers/tests using the old handler name. */
  async handleCron(): Promise<void> {
    await this.handleSignalCron();
  }

  /** Test helper: is the overlap guard currently set? */
  get running(): boolean {
    return this.activeTask !== null;
  }

  /** Test helper: force the running flag (e.g. simulate long run). */
  setRunning(value: boolean): void {
    this.activeTask = value ? 'signals' : null;
  }

  getCronExpression(): string {
    return this.getSignalCronExpression();
  }

  getSignalCronExpression(): string {
    return this.config.signalCronExpression;
  }

  getAuthRefreshCronExpression(): string {
    return this.config.authRefreshCronExpression;
  }

  isJobRegistered(name = 'extractSignals'): boolean {
    try {
      return !!this.schedulerRegistry.getCronJob(name);
    } catch {
      return false;
    }
  }
}
