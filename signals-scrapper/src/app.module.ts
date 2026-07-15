import { Module } from '@nestjs/common';
import { ScheduleModule } from '@nestjs/schedule';
import { BrowserService } from './browser/browser.service';
import { ChromeLauncherService } from './browser/chrome-launcher.service';
import { AppConfigModule } from './config/config.module';
import { DedupService } from './dedup/dedup.service';
import { JsonlLoggerService } from './logging/jsonl-logger.service';
import { Mt5ExecutionService } from './mt5/mt5-execution.service';
import { OpenAiVisionService } from './openai/openai-vision.service';
import { AutochartistExtractor } from './scraper/extractors/autochartist.extractor';
import { TradingCentralExtractor } from './scraper/extractors/trading-central.extractor';
import { ScraperService } from './scraper/scraper.service';
import { SchedulerService } from './scheduler/scheduler.service';
import { ShutdownService } from './shutdown.service';

@Module({
  imports: [AppConfigModule, ScheduleModule.forRoot()],
  providers: [
    BrowserService,
    ChromeLauncherService,
    DedupService,
    JsonlLoggerService,
    OpenAiVisionService,
    Mt5ExecutionService,
    TradingCentralExtractor,
    AutochartistExtractor,
    ScraperService,
    SchedulerService,
    ShutdownService,
  ],
})
export class AppModule {}
