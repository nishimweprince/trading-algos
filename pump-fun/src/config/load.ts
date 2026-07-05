import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { parse as parseYaml } from 'yaml';
import { config as loadDotenv } from 'dotenv';
import { ConfigSchema, type Config } from './schema.ts';

loadDotenv();

/**
 * Resolve `${ENV_VAR}` placeholders in string values against process.env.
 * Used so config.yaml can reference secret-bearing endpoints (RPC URLs with
 * tokens) without the secret ever living in the file.
 */
function interpolateEnv(value: unknown): unknown {
  if (typeof value === 'string') {
    return value.replace(/\$\{([A-Z0-9_]+)\}/g, (_match, name: string) => {
      const resolved = process.env[name];
      if (resolved === undefined) {
        throw new ConfigError(
          `config references \${${name}} but that env var is not set (check your .env)`,
        );
      }
      return resolved;
    });
  }
  if (Array.isArray(value)) return value.map(interpolateEnv);
  if (value && typeof value === 'object') {
    return Object.fromEntries(
      Object.entries(value).map(([k, v]) => [k, interpolateEnv(v)]),
    );
  }
  return value;
}

export class ConfigError extends Error {
  override name = 'ConfigError';
}

export interface LoadOptions {
  /** Path to config.yaml. Defaults to ./config.yaml relative to cwd. */
  path?: string;
}

export function loadConfig(opts: LoadOptions = {}): Config {
  const path = resolve(opts.path ?? 'config.yaml');

  let raw: string;
  try {
    raw = readFileSync(path, 'utf8');
  } catch (err) {
    throw new ConfigError(
      `could not read config file at ${path}: ${(err as Error).message}`,
    );
  }

  let parsed: unknown;
  try {
    parsed = parseYaml(raw);
  } catch (err) {
    throw new ConfigError(`config at ${path} is not valid YAML: ${(err as Error).message}`);
  }

  const interpolated = interpolateEnv(parsed);
  const result = ConfigSchema.safeParse(interpolated);
  if (!result.success) {
    const issues = result.error.issues
      .map((i) => `  - ${i.path.join('.') || '(root)'}: ${i.message}`)
      .join('\n');
    throw new ConfigError(`config at ${path} failed validation:\n${issues}`);
  }

  return result.data;
}

/**
 * Read a required secret from the environment. Centralizes the "secrets come
 * from env, never config/DB" rule and produces a clear error when missing.
 */
export function readSecret(envVar: string, opts: { required?: boolean } = {}): string | undefined {
  const value = process.env[envVar];
  if (!value && opts.required) {
    throw new ConfigError(`required secret env var ${envVar} is not set`);
  }
  return value || undefined;
}
