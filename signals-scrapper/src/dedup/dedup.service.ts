import { Injectable, Logger, OnModuleDestroy, OnModuleInit } from '@nestjs/common';
import { AppConfigService } from '../config/app-config.service';
import { TradingIdea } from '../models/trading-idea.model';
import {
  Mt5ExecutionRecord,
  Mt5ExecutionStatus,
} from '../mt5/mt5.types';
import { computeIdeaHash } from './hash';
import { DebugRunRecord, SeenStore } from './seen-store';

@Injectable()
export class DedupService implements OnModuleInit, OnModuleDestroy {
  private readonly logger = new Logger(DedupService.name);
  private store!: SeenStore;

  constructor(private readonly config: AppConfigService) {}

  onModuleInit(): void {
    this.store = new SeenStore(
      this.config.seenStatePath,
      this.config.seenMaxEntries,
      this.config.debugRunMaxEntries,
      this.config.mt5ExecutionMaxEntries,
    );
    this.store.load();
    this.logger.log(
      `Seen state loaded: ${this.store.size()} hash(es) from ${this.config.seenStatePath}`,
    );
  }

  onModuleDestroy(): void {
    this.flush();
  }

  /** Ensure store is ready (for tests that inject without Nest lifecycle). */
  ensureLoaded(): void {
    if (!this.store) {
      this.onModuleInit();
    }
  }

  /**
   * Assign hashes to ideas that lack them, then return only those not yet seen.
   * Does NOT mark them as seen — call markSeen after successful log.
   */
  filterNew(ideas: TradingIdea[]): TradingIdea[] {
    this.ensureLoaded();
    const withHashes = ideas.map((idea) => {
      if (idea.hash) return idea;
      return { ...idea, hash: computeIdeaHash(idea) };
    });
    return withHashes.filter((idea) => !this.store.has(idea.hash));
  }

  markSeen(
    ideas: TradingIdea[],
    executions: Mt5ExecutionRecord[] = [],
  ): void {
    this.ensureLoaded();
    const now = new Date().toISOString();
    this.store.addIdeas(ideas, now);
    this.store.addExecutions(executions);
    this.store.save();
  }

  /** Persist new signals and the successful extraction diagnostics together. */
  persistSuccess(
    ideas: TradingIdea[],
    run: DebugRunRecord,
    executions: Mt5ExecutionRecord[] = [],
  ): void {
    this.ensureLoaded();
    this.store.addIdeas(ideas, new Date().toISOString());
    this.store.addRun(run);
    this.store.addExecutions(executions);
    this.store.save();
  }

  /** Persist diagnostics without marking any signal as seen. */
  recordRun(run: DebugRunRecord): void {
    this.ensureLoaded();
    this.store.addRun(run);
    this.store.save();
  }

  listExecutions(...statuses: Mt5ExecutionStatus[]): Mt5ExecutionRecord[] {
    this.ensureLoaded();
    return this.store.listExecutions(...statuses);
  }

  updateExecution(
    signalId: string,
    patch: Parameters<SeenStore['updateExecution']>[1],
  ): Mt5ExecutionRecord {
    this.ensureLoaded();
    const updated = this.store.updateExecution(signalId, patch);
    this.store.save();
    return updated;
  }

  flush(): void {
    if (this.store) {
      this.store.save();
      this.logger.log(`Seen state flushed (${this.store.size()} hashes)`);
    }
  }

  /** Test/helper access. */
  getStore(): SeenStore {
    this.ensureLoaded();
    return this.store;
  }
}

export { computeIdeaHash };
