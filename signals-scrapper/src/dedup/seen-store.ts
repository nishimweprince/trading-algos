import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'fs';
import { dirname } from 'path';

export interface SeenStateFile {
  /** Hash -> first-seen ISO timestamp (for pruning by age if needed). */
  hashes: Record<string, string>;
  updatedAt: string;
}

/**
 * File-backed set of seen idea hashes. Caps growth by keeping the most recent
 * SEEN_MAX_ENTRIES (by first-seen timestamp).
 */
export class SeenStore {
  private hashes = new Map<string, string>();

  constructor(
    private readonly filePath: string,
    private readonly maxEntries: number = 5000,
  ) {}

  load(): void {
    if (!existsSync(this.filePath)) {
      this.hashes = new Map();
      return;
    }
    const raw = readFileSync(this.filePath, 'utf8');
    if (!raw.trim()) {
      this.hashes = new Map();
      return;
    }
    const data = JSON.parse(raw) as SeenStateFile;
    this.hashes = new Map(Object.entries(data.hashes ?? {}));
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

  size(): number {
    return this.hashes.size;
  }

  /** Keep only the most recent maxEntries by first-seen time. */
  prune(): void {
    if (this.hashes.size <= this.maxEntries) return;
    const entries = [...this.hashes.entries()].sort((a, b) =>
      a[1] < b[1] ? -1 : a[1] > b[1] ? 1 : 0,
    );
    const drop = entries.length - this.maxEntries;
    this.hashes = new Map(entries.slice(drop));
  }

  save(): void {
    this.prune();
    const dir = dirname(this.filePath);
    if (!existsSync(dir)) {
      mkdirSync(dir, { recursive: true });
    }
    const payload: SeenStateFile = {
      hashes: Object.fromEntries(this.hashes),
      updatedAt: new Date().toISOString(),
    };
    writeFileSync(this.filePath, JSON.stringify(payload, null, 2), 'utf8');
  }

  /** Snapshot of hashes for tests. */
  allHashes(): string[] {
    return [...this.hashes.keys()];
  }
}
