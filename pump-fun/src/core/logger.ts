/**
 * Structured JSON logger with secret redaction (Section 8).
 *
 * Secrets are registered at startup; any registered value appearing anywhere in
 * a log line (message, fields, or a serialized error/stack) is replaced with
 * [REDACTED] before it is written. This is defense-in-depth: code should never
 * pass secrets to the logger in the first place, but a stray error trace must
 * not leak the wallet key.
 */

type Level = 'debug' | 'info' | 'warn' | 'error';

const LEVEL_ORDER: Record<Level, number> = { debug: 0, info: 1, warn: 2, error: 3 };

const secrets = new Set<string>();

/** Register a secret value to be redacted from all future log output. */
export function registerSecret(value: string | undefined | null): void {
  // Ignore short/empty values to avoid redacting common substrings.
  if (value && value.length >= 8) secrets.add(value);
}

function redact(text: string): string {
  let out = text;
  for (const secret of secrets) {
    if (out.includes(secret)) out = out.split(secret).join('[REDACTED]');
  }
  return out;
}

function serializeField(value: unknown): unknown {
  if (value instanceof Error) {
    return { name: value.name, message: value.message, stack: value.stack };
  }
  if (typeof value === 'bigint') return value.toString();
  return value;
}

let minLevel: Level = (process.env.LOG_LEVEL as Level) ?? 'info';

export function setLogLevel(level: Level): void {
  minLevel = level;
}

export interface Logger {
  debug(msg: string, fields?: Record<string, unknown>): void;
  info(msg: string, fields?: Record<string, unknown>): void;
  warn(msg: string, fields?: Record<string, unknown>): void;
  error(msg: string, fields?: Record<string, unknown>): void;
  child(bindings: Record<string, unknown>): Logger;
}

function write(level: Level, base: Record<string, unknown>, msg: string, fields?: Record<string, unknown>): void {
  if (LEVEL_ORDER[level] < LEVEL_ORDER[minLevel]) return;

  const record: Record<string, unknown> = {
    ts: new Date().toISOString(),
    level,
    msg,
    ...base,
  };
  if (fields) {
    for (const [k, v] of Object.entries(fields)) record[k] = serializeField(v);
  }

  const line = redact(JSON.stringify(record));
  if (level === 'error') process.stderr.write(line + '\n');
  else process.stdout.write(line + '\n');
}

function make(base: Record<string, unknown>): Logger {
  return {
    debug: (msg, fields) => write('debug', base, msg, fields),
    info: (msg, fields) => write('info', base, msg, fields),
    warn: (msg, fields) => write('warn', base, msg, fields),
    error: (msg, fields) => write('error', base, msg, fields),
    child: (bindings) => make({ ...base, ...bindings }),
  };
}

export const logger: Logger = make({});
