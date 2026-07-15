import { Injectable, Logger, OnModuleInit } from '@nestjs/common';
import {
  AppConfig,
  assertRuntimeConfig,
  loadAppConfig,
  SourceConfig,
} from './sources.schema';

@Injectable()
export class AppConfigService implements OnModuleInit {
  private readonly logger = new Logger(AppConfigService.name);
  private config!: AppConfig;

  onModuleInit(): void {
    this.config = loadAppConfig(process.env);
    assertRuntimeConfig(this.config);
    this.logger.log(
      `Config loaded: ${this.config.SOURCES.length} source(s), mode=${this.config.BROWSER_MODE}, signalsCron=${this.config.SIGNAL_CRON_EXPRESSION}, authRefreshCron=${this.config.AUTH_REFRESH_CRON_EXPRESSION}`,
    );
  }

  /** Force-load config (also used by tests and early boot). */
  load(env: NodeJS.ProcessEnv = process.env): AppConfig {
    this.config = loadAppConfig(env);
    return this.config;
  }

  get snapshot(): AppConfig {
    if (!this.config) {
      this.config = loadAppConfig(process.env);
    }
    return this.config;
  }

  get sources(): SourceConfig[] {
    return this.snapshot.SOURCES;
  }

  get browserMode(): AppConfig['BROWSER_MODE'] {
    return this.snapshot.BROWSER_MODE;
  }

  get cdpEndpoint(): string {
    return this.snapshot.CDP_ENDPOINT;
  }

  get cdpAutoStart(): boolean {
    return this.snapshot.CDP_AUTO_START;
  }

  get cdpStartupTimeoutMs(): number {
    return this.snapshot.CDP_STARTUP_TIMEOUT_MS;
  }

  get hostOs(): AppConfig['HOST_OS'] {
    return this.snapshot.HOST_OS;
  }

  get chromeExecutablePath(): string {
    return this.snapshot.CHROME_EXECUTABLE_PATH;
  }

  get userDataDir(): string {
    return this.snapshot.USER_DATA_DIR;
  }

  get cronExpression(): string {
    return this.signalCronExpression;
  }

  get signalCronExpression(): string {
    return this.snapshot.SIGNAL_CRON_EXPRESSION;
  }

  get authRefreshCronExpression(): string {
    return this.snapshot.AUTH_REFRESH_CRON_EXPRESSION;
  }

  get ideasLogPath(): string {
    return this.snapshot.IDEAS_LOG_PATH;
  }

  get screenshotDir(): string {
    return this.snapshot.SCREENSHOT_DIR;
  }

  get seenStatePath(): string {
    return this.snapshot.SEEN_STATE_PATH;
  }

  get headless(): boolean {
    return this.snapshot.HEADLESS;
  }

  get navTimeoutMs(): number {
    return this.snapshot.NAV_TIMEOUT_MS;
  }

  get contentWaitMs(): number {
    return this.snapshot.CONTENT_WAIT_MS;
  }

  get seenMaxEntries(): number {
    return this.snapshot.SEEN_MAX_ENTRIES;
  }

  get debugRunMaxEntries(): number {
    return this.snapshot.DEBUG_RUN_MAX_ENTRIES;
  }

  get openaiApiKey(): string {
    return this.snapshot.OPENAI_API_KEY;
  }

  get openaiModel(): string {
    return this.snapshot.OPENAI_MODEL;
  }

  get openaiTimeoutMs(): number {
    return this.snapshot.OPENAI_TIMEOUT_MS;
  }

  get mt5SignalTradingEnabled(): boolean {
    return this.snapshot.MT5_SIGNAL_TRADING_ENABLED;
  }

  get mt5SignalApiUrl(): string {
    return this.snapshot.MT5_SIGNAL_API_URL;
  }

  get mt5SignalApiKey(): string {
    return this.snapshot.MT5_SIGNAL_API_KEY;
  }

  get mt5SignalTimeoutMs(): number {
    return this.snapshot.MT5_SIGNAL_TIMEOUT_MS;
  }

  get mt5ExecutionMaxEntries(): number {
    return this.snapshot.MT5_EXECUTION_MAX_ENTRIES;
  }

  get mt5SignalRules(): AppConfig['MT5_SIGNAL_RULES'] {
    return this.snapshot.MT5_SIGNAL_RULES;
  }
}
