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
      `Config loaded: ${this.config.SOURCES.length} source(s), mode=${this.config.BROWSER_MODE}, cron=${this.config.CRON_EXPRESSION}`,
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

  get userDataDir(): string {
    return this.snapshot.USER_DATA_DIR;
  }

  get cronExpression(): string {
    return this.snapshot.CRON_EXPRESSION;
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

  get ollamaApiKey(): string {
    return this.snapshot.OLLAMA_API_KEY;
  }

  get ollamaModel(): string {
    return this.snapshot.OLLAMA_MODEL;
  }

  get ollamaHost(): string {
    return this.snapshot.OLLAMA_HOST;
  }

  get ollamaTimeoutMs(): number {
    return this.snapshot.OLLAMA_TIMEOUT_MS;
  }
}
