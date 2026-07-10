import { Injectable, Logger } from '@nestjs/common';
import { appendFileSync, existsSync, mkdirSync, readFileSync } from 'fs';
import { dirname } from 'path';
import { AppConfigService } from '../config/app-config.service';
import { TradingIdea } from '../models/trading-idea.model';

@Injectable()
export class JsonlLoggerService {
  private readonly logger = new Logger(JsonlLoggerService.name);

  constructor(private readonly config: AppConfigService) {}

  /**
   * Append each idea as one JSON object per line to IDEAS_LOG_PATH.
   * Returns the number of lines written.
   */
  appendIdeas(ideas: TradingIdea[]): number {
    if (ideas.length === 0) return 0;
    const path = this.config.ideasLogPath;
    const dir = dirname(path);
    if (!existsSync(dir)) {
      mkdirSync(dir, { recursive: true });
    }
    const lines = ideas.map((idea) => JSON.stringify(idea)).join('\n') + '\n';
    appendFileSync(path, lines, 'utf8');
    this.logger.log(`Appended ${ideas.length} idea(s) to ${path}`);
    return ideas.length;
  }

  /** Read all logged ideas (for tests / inspection). */
  readAll(): TradingIdea[] {
    const path = this.config.ideasLogPath;
    if (!existsSync(path)) return [];
    const raw = readFileSync(path, 'utf8');
    if (!raw.trim()) return [];
    return raw
      .split('\n')
      .filter((line) => line.trim().length > 0)
      .map((line) => JSON.parse(line) as TradingIdea);
  }
}
