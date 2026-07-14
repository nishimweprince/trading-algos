import { Injectable, Logger, OnModuleDestroy } from '@nestjs/common';
import { mkdirSync } from 'fs';
import { join } from 'path';
import { createWorker, OEM, PSM, Worker } from 'tesseract.js';

export interface OcrResult {
  text: string;
  positionalText: string;
  confidence: number;
  tsv?: string;
}

export class OcrExtractionError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'OcrExtractionError';
  }
}

interface TsvWord {
  key: string;
  left: number;
  top: number;
  text: string;
}

/** Convert Tesseract TSV into text lines with coordinates for multi-column cards. */
export function tsvToPositionalText(tsv: string | null | undefined): string {
  if (!tsv?.trim()) return '';
  const words: TsvWord[] = [];
  for (const row of tsv.split('\n').slice(1)) {
    const columns = row.split('\t');
    if (columns.length < 12 || columns[0] !== '5') continue;
    const text = columns.slice(11).join('\t').trim();
    if (!text) continue;
    words.push({
      key: `${columns[2]}:${columns[3]}:${columns[4]}`,
      left: Number(columns[6]) || 0,
      top: Number(columns[7]) || 0,
      text,
    });
  }

  const lines = new Map<string, TsvWord[]>();
  for (const word of words) {
    const line = lines.get(word.key) ?? [];
    line.push(word);
    lines.set(word.key, line);
  }

  return [...lines.values()]
    .map((line) => line.sort((a, b) => a.left - b.left))
    .sort((a, b) => {
      const vertical = a[0].top - b[0].top;
      return Math.abs(vertical) > 4 ? vertical : a[0].left - b[0].left;
    })
    .map(
      (line) =>
        `[x=${line[0].left},y=${line[0].top}] ${line.map((word) => word.text).join(' ')}`,
    )
    .join('\n');
}

@Injectable()
export class OcrService implements OnModuleDestroy {
  private readonly logger = new Logger(OcrService.name);
  private workerPromise: Promise<Worker> | null = null;

  async recognize(screenshotPath: string): Promise<OcrResult> {
    const worker = await this.getWorker();
    const result = await worker.recognize(
      screenshotPath,
      { rotateAuto: true },
      { text: true, tsv: true },
    );
    const text = result.data.text?.trim() ?? '';
    if (!text) {
      throw new OcrExtractionError('Tesseract returned no text');
    }
    return {
      text,
      positionalText: tsvToPositionalText(result.data.tsv),
      confidence: result.data.confidence,
      tsv: result.data.tsv ?? undefined,
    };
  }

  async onModuleDestroy(): Promise<void> {
    if (!this.workerPromise) return;
    try {
      const worker = await this.workerPromise;
      await worker.terminate();
    } catch (err) {
      this.logger.warn(
        `OCR worker shutdown failed: ${err instanceof Error ? err.message : String(err)}`,
      );
    } finally {
      this.workerPromise = null;
    }
  }

  private getWorker(): Promise<Worker> {
    if (!this.workerPromise) {
      this.workerPromise = this.createConfiguredWorker().catch((err) => {
        this.workerPromise = null;
        throw err;
      });
    }
    return this.workerPromise;
  }

  private async createConfiguredWorker(): Promise<Worker> {
    this.logger.log('Initializing reusable Tesseract.js English worker');
    const cachePath = join(process.cwd(), 'data', 'tesseract-cache');
    mkdirSync(cachePath, { recursive: true });
    const worker = await createWorker('eng', OEM.LSTM_ONLY, {
      cachePath,
      logger: (message) => {
        if (message.status === 'recognizing text') {
          this.logger.debug(
            `OCR progress: ${Math.round(message.progress * 100)}%`,
          );
        }
      },
    });
    await worker.setParameters({
      tessedit_pageseg_mode: PSM.AUTO,
      preserve_interword_spaces: '1',
      user_defined_dpi: '300',
    });
    return worker;
  }
}
