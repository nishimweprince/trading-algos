/** GramJS: deep imports for errors / message types (no package exports typings). */
declare module 'telegram/errors' {
  export class FloodWaitError extends Error {
    seconds: number;
    constructor(args: unknown);
  }
}

declare module 'telegram/tl/custom/message' {
  export class CustomMessage {
    id: number | bigint | { toString(): string };
    date?: Date | number;
    get text(): string;
  }
}

declare module 'input' {
  const input: { text(prompt: string): Promise<string> };
  export default input;
}
