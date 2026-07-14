import {
  existsSync,
  mkdirSync,
  readFileSync,
  renameSync,
  writeFileSync,
} from 'fs';
import { dirname } from 'path';
import {
  Mt5ExecutionRecord,
  Mt5ExecutionStatus,
} from '../mt5/mt5.types';
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
  version?: 1 | 2 | 3;
  /** Hash -> first-seen ISO timestamp (for pruning by age if needed). */
  hashes: Record<string, string>;
  signals?: Record<string, SeenSignalRecord>;
  runs?: DebugRunRecord[];
  executions?: Record<string, Mt5ExecutionRecord>;
  updatedAt: string;
}

const PRUNABLE_EXECUTION_STATUSES = new Set<Mt5ExecutionStatus>([
  'succeeded',
  'rejected',
  'skipped',
]);

/**
 * File-backed set of seen idea hashes. Caps growth by keeping the most recent
 * SEEN_MAX_ENTRIES (by first-seen timestamp).
 */
export class SeenStore {
  private hashes = new Map<string, string>();
  private signals = new Map<string, SeenSignalRecord>();
  private runs: DebugRunRecord[] = [];
  private executions = new Map<string, Mt5ExecutionRecord>();

  constructor(
    private readonly filePath: string,
    private readonly maxEntries: number = 5000,
    private readonly maxDebugRuns: number = 100,
    private readonly maxExecutions: number = 5000,
  ) {}

  load(): void {
    if (!existsSync(this.filePath)) {
      this.hashes = new Map();
      this.signals = new Map();
      this.runs = [];
      this.executions = new Map();
      return;
    }
    const raw = readFileSync(this.filePath, 'utf8');
    if (!raw.trim()) {
      this.hashes = new Map();
      this.signals = new Map();
      this.runs = [];
      this.executions = new Map();
      return;
    }
    const data = JSON.parse(raw) as SeenStateFile;
    this.hashes = new Map(Object.entries(data.hashes ?? {}));
    this.signals = new Map(Object.entries(data.signals ?? {}));
    this.runs = Array.isArray(data.runs) ? data.runs : [];
    this.executions = new Map(Object.entries(data.executions ?? {}));
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

  /** Insert new execution records without ever replacing their frozen requests. */
  addExecutions(records: Mt5ExecutionRecord[]): void {
    for (const record of records) {
      if (!this.executions.has(record.signalId)) {
        this.executions.set(record.signalId, structuredClone(record));
      }
    }
  }

  getExecution(signalId: string): Mt5ExecutionRecord | undefined {
    const record = this.executions.get(signalId);
    return record ? structuredClone(record) : undefined;
  }

  listExecutions(...statuses: Mt5ExecutionStatus[]): Mt5ExecutionRecord[] {
    const allowed = new Set(statuses);
    return [...this.executions.values()]
      .filter((record) => allowed.size === 0 || allowed.has(record.status))
      .map((record) => structuredClone(record));
  }

  updateExecution(
    signalId: string,
    patch: Partial<
      Omit<
        Mt5ExecutionRecord,
        'signalId' | 'ideaHash' | 'request' | 'createdAt'
      >
    >,
  ): Mt5ExecutionRecord {
    const current = this.executions.get(signalId);
    if (!current) {
      throw new Error(`Unknown MT5 execution record: ${signalId}`);
    }
    const updated: Mt5ExecutionRecord = {
      ...current,
      ...structuredClone(patch),
      signalId: current.signalId,
      ideaHash: current.ideaHash,
      request: current.request,
      createdAt: current.createdAt,
      updatedAt: patch.updatedAt ?? new Date().toISOString(),
    };
    this.executions.set(signalId, updated);
    return structuredClone(updated);
  }

  size(): number {
    return this.hashes.size;
  }

  /** Keep only the most recent maxEntries by first-seen time. */
  prune(): void {
    const protectedHashes = new Set(
      [...this.executions.values()]
        .filter((record) => !PRUNABLE_EXECUTION_STATUSES.has(record.status))
        .map((record) => record.ideaHash),
    );
    if (this.hashes.size > this.maxEntries) {
      const entries = [...this.hashes.entries()].sort((a, b) =>
        a[1] < b[1] ? -1 : a[1] > b[1] ? 1 : 0,
      );
      const drop = entries.length - this.maxEntries;
      const newest = entries.slice(drop);
      const kept = entries.filter(
        ([hash]) =>
          protectedHashes.has(hash) || newest.some(([keptHash]) => keptHash === hash),
      );
      const keptHashes = new Set(kept.map(([hash]) => hash));
      this.hashes = new Map(kept);
      this.signals = new Map(
        [...this.signals.entries()].filter(([hash]) => keptHashes.has(hash)),
      );
    }
    if (this.runs.length > this.maxDebugRuns) {
      this.runs = this.runs.slice(-this.maxDebugRuns);
    }
    const terminal = [...this.executions.entries()]
      .filter(([, record]) => PRUNABLE_EXECUTION_STATUSES.has(record.status))
      .sort((a, b) => a[1].updatedAt.localeCompare(b[1].updatedAt));
    const terminalToDrop = Math.max(0, terminal.length - this.maxExecutions);
    for (const [signalId] of terminal.slice(0, terminalToDrop)) {
      this.executions.delete(signalId);
    }
  }

  save(): void {
    this.prune();
    const dir = dirname(this.filePath);
    if (!existsSync(dir)) {
      mkdirSync(dir, { recursive: true });
    }
    const payload: SeenStateFile = {
      version: 3,
      hashes: Object.fromEntries(this.hashes),
      signals: Object.fromEntries(this.signals),
      runs: this.runs,
      executions: Object.fromEntries(this.executions),
      updatedAt: new Date().toISOString(),
    };
    const temporaryPath = `${this.filePath}.${process.pid}.tmp`;
    writeFileSync(temporaryPath, JSON.stringify(payload, null, 2), 'utf8');
    renameSync(temporaryPath, this.filePath);
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

  allExecutions(): Record<string, Mt5ExecutionRecord> {
    return Object.fromEntries(
      [...this.executions.entries()].map(([key, value]) => [
        key,
        structuredClone(value),
      ]),
    );
  }
}
