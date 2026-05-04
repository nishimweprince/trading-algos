import fs from 'node:fs/promises';
import path from 'node:path';

const MAX_PER_CHANNEL = 5000;

export type HandledFile = Record<string, string[]>;

function key(channelId: string, messageId: number): string {
  return `${channelId}:${messageId}`;
}

export class Deduper {
  private readonly set = new Set<string>();
  private readonly channelOrder = new Map<string, number[]>();

  constructor(initial?: HandledFile) {
    if (initial) {
      for (const [ch, ids] of Object.entries(initial)) {
        const nums = ids.map((x) => Number(x)).filter((n) => Number.isFinite(n));
        for (const id of nums) {
          this.addToMemoryOnly(ch, id);
        }
      }
    }
  }

  private trimChannel(ch: string): void {
    const arr = this.channelOrder.get(ch);
    if (!arr || arr.length <= MAX_PER_CHANNEL) return;
    const drop = arr.splice(0, arr.length - MAX_PER_CHANNEL);
    for (const id of drop) {
      this.set.delete(key(ch, id));
    }
  }

  private addToMemoryOnly(channelId: string, messageId: number): void {
    const k = key(channelId, messageId);
    if (this.set.has(k)) return;
    this.set.add(k);
    const arr = this.channelOrder.get(channelId) ?? [];
    arr.push(messageId);
    this.channelOrder.set(channelId, arr);
    this.trimChannel(channelId);
  }

  alreadyHandled(channelId: string, messageId: number): boolean {
    return this.set.has(key(channelId, messageId));
  }

  /** Mark handled in memory only (cursor persistence covers forward progress). */
  markHandledMemory(channelId: string, messageId: number): void {
    this.addToMemoryOnly(channelId, messageId);
  }

  toFile(): HandledFile {
    const out: HandledFile = {};
    for (const [ch, ids] of this.channelOrder) {
      out[ch] = ids.map(String);
    }
    return out;
  }

  static async load(filePath: string): Promise<Deduper> {
    try {
      const raw = await fs.readFile(filePath, 'utf8');
      const parsed = JSON.parse(raw) as HandledFile;
      return new Deduper(parsed && typeof parsed === 'object' ? parsed : {});
    } catch (e) {
      const err = e as NodeJS.ErrnoException;
      if (err.code === 'ENOENT') return new Deduper({});
      throw e;
    }
  }

  static async persistAtomic(filePath: string, data: HandledFile): Promise<void> {
    const dir = path.dirname(filePath);
    await fs.mkdir(dir, { recursive: true });
    const tmp = `${filePath}.${process.pid}.${Date.now()}.tmp`;
    const json = `${JSON.stringify(data, null, 2)}\n`;
    await fs.writeFile(tmp, json, 'utf8');
    await fs.rename(tmp, filePath);
  }
}
