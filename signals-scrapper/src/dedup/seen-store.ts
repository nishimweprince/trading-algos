import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'fs';
import { dirname } from 'path';
import { ProviderType, TradingIdea } from '../models/trading-idea.model';

export type DebugRunStage =
  | 'browser'
  | 'navigation'
  | 'login'
  | 'screenshot'
  | 'ocr'
  | 'ollama'
  | 'validation'
  | 'persistence'
  | 'complete';

export interface DebugRunRecord {
  id: string;
  provider: ProviderType;
  sourceUrl: string;
  capturedAt: string;
  status: 'success' | 'failed';
  stage: DebugRunStage;
  screenshotPath?: string;
  ocr?: {
    text: string;
    positionalText?: string;
    confidence?: number;
  };
  ollama?: {
    model: string;
    rawResponse?: string;
    repaired?: boolean;
  };
  signals?: TradingIdea[];
  rejected?: Array<{ source?: string; reasons: string[] }>;
  error?: string;
}

export interface SeenSignalRecord {
  firstSeenAt: string;
  signal: TradingIdea;
}

export interface SeenStateFile {
  version?: 1 | 2;
  /** Hash -> first-seen ISO timestamp (for pruning by age if needed). */
  hashes: Record<string, string>;
  signals?: Record<string, SeenSignalRecord>;
  runs?: DebugRunRecord[];
  updatedAt: string;
}

/**
 * File-backed set of seen idea hashes. Caps growth by keeping the most recent
 * SEEN_MAX_ENTRIES (by first-seen timestamp).
 */
export class SeenStore {
  private hashes = new Map<string, string>();
  private signals = new Map<string, SeenSignalRecord>();
  private runs: DebugRunRecord[] = [];

  constructor(
    private readonly filePath: string,
    private readonly maxEntries: number = 5000,
    private readonly maxDebugRuns: number = 100,
  ) {}

  load(): void {
    if (!existsSync(this.filePath)) {
      this.hashes = new Map();
      this.signals = new Map();
      this.runs = [];
      return;
    }
    const raw = readFileSync(this.filePath, 'utf8');
    if (!raw.trim()) {
      this.hashes = new Map();
      this.signals = new Map();
      this.runs = [];
      return;
    }
    const data = JSON.parse(raw) as SeenStateFile;
    this.hashes = new Map(Object.entries(data.hashes ?? {}));
    this.signals = new Map(Object.entries(data.signals ?? {}));
    this.runs = Array.isArray(data.runs) ? data.runs : [];
    this.prune();
  }

  has(hash: string): boolean {
    return this.hashes.has(hash);
  }

  add(hash: string, seenAt: string = new Date().toISOString()): void {
    if (!this.hashes.has(hash)) {
      this.hashes.set(hash, seenAt);
    }
  }

  addMany(hashes: string[], seenAt: string = new Date().toISOString()): void {
    for (const h of hashes) {
      this.add(h, seenAt);
    }
  }

  addIdeas(
    ideas: TradingIdea[],
    seenAt: string = new Date().toISOString(),
  ): void {
    for (const idea of ideas) {
      this.add(idea.hash, seenAt);
      if (!this.signals.has(idea.hash)) {
        this.signals.set(idea.hash, { firstSeenAt: seenAt, signal: idea });
      }
    }
  }

  addRun(run: DebugRunRecord): void {
    this.runs.push(run);
    if (this.runs.length > this.maxDebugRuns) {
      this.runs = this.runs.slice(-this.maxDebugRuns);
    }
  }

  size(): number {
    return this.hashes.size;
  }

  /** Keep only the most recent maxEntries by first-seen time. */
  prune(): void {
    if (this.hashes.size > this.maxEntries) {
      const entries = [...this.hashes.entries()].sort((a, b) =>
        a[1] < b[1] ? -1 : a[1] > b[1] ? 1 : 0,
      );
      const drop = entries.length - this.maxEntries;
      const kept = entries.slice(drop);
      const keptHashes = new Set(kept.map(([hash]) => hash));
      this.hashes = new Map(kept);
      this.signals = new Map(
        [...this.signals.entries()].filter(([hash]) => keptHashes.has(hash)),
      );
    }
    if (this.runs.length > this.maxDebugRuns) {
      this.runs = this.runs.slice(-this.maxDebugRuns);
    }
  }

  save(): void {
    this.prune();
    const dir = dirname(this.filePath);
    if (!existsSync(dir)) {
      mkdirSync(dir, { recursive: true });
    }
    const payload: SeenStateFile = {
      version: 2,
      hashes: Object.fromEntries(this.hashes),
      signals: Object.fromEntries(this.signals),
      runs: this.runs,
      updatedAt: new Date().toISOString(),
    };
    writeFileSync(this.filePath, JSON.stringify(payload, null, 2), 'utf8');
  }

  /** Snapshot of hashes for tests. */
  allHashes(): string[] {
    return [...this.hashes.keys()];
  }

  allSignals(): Record<string, SeenSignalRecord> {
    return Object.fromEntries(this.signals);
  }

  allRuns(): DebugRunRecord[] {
    return [...this.runs];
  }
}
