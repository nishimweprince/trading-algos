import fs from 'node:fs/promises';
import path from 'node:path';

let writeChain = Promise.resolve();

export function createJsonlWriter(filePath: string) {
  const dir = path.dirname(filePath);

  function appendLine(obj: unknown): void {
    const line = `${JSON.stringify(obj)}\n`;
    writeChain = writeChain.then(async () => {
      await fs.mkdir(dir, { recursive: true });
      await fs.appendFile(filePath, line, 'utf8');
    });
  }

  return {
    append(obj: unknown): void {
      appendLine(obj);
    },
    async flush(): Promise<void> {
      await writeChain;
    },
  };
}

export type JsonlWriter = ReturnType<typeof createJsonlWriter>;
