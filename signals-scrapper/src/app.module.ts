import { Module } from '@nestjs/common';
import { ScheduleModule } from '@nestjs/schedule';
import { BrowserService } from './browser/browser.service';
import { AppConfigModule } from './config/config.module';
import { DedupService } from './dedup/dedup.service';
import { JsonlLoggerService } from './logging/jsonl-logger.service';
import { AutochartistExtractor } from './scraper/extractors/autochartist.extractor';
import { TradingCentralExtractor } from './scraper/extractors/trading-central.extractor';
import { ScraperService } from './scraper/scraper.service';
import { SchedulerService } from './scheduler/scheduler.service';
import { ShutdownService } from './shutdown.service';

@Module({
  imports: [AppConfigModule, ScheduleModule.forRoot()],
  providers: [
    BrowserService,
    DedupService,
    JsonlLoggerService,
    TradingCentralExtractor,
    AutochartistExtractor,
    ScraperService,
    SchedulerService,
    ShutdownService,
  ],
})
export class AppModule {}
