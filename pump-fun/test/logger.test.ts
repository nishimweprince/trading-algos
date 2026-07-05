import { describe, it, expect, vi, afterEach } from 'vitest';
import { logger, registerSecret, setLogLevel } from '../src/core/logger.ts';

describe('logger secret redaction', () => {
  afterEach(() => vi.restoreAllMocks());

  it('redacts a registered secret from message and fields', () => {
    setLogLevel('debug');
    const secret = 'super-secret-wallet-key-abcdef1234567890';
    registerSecret(secret);

    const out = vi.spyOn(process.stdout, 'write').mockImplementation(() => true);
    logger.info(`leaking ${secret} here`, { key: secret, nested: { k: secret } });

    const line = out.mock.calls[0]?.[0] as string;
    expect(line).not.toContain(secret);
    expect(line).toContain('[REDACTED]');
  });

  it('redacts a secret embedded in an error stack', () => {
    const secret = 'another-long-secret-value-0987654321';
    registerSecret(secret);
    const err = new Error(`boom with ${secret}`);
    const out = vi.spyOn(process.stderr, 'write').mockImplementation(() => true);
    logger.error('failure', { err });
    const line = out.mock.calls[0]?.[0] as string;
    expect(line).not.toContain(secret);
  });
});
