import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import fs from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import type { AppConfig } from '../src/config.js';
import { createJsonlWriter } from '../src/state/jsonlLog.js';
import { Deduper } from '../src/signals/deduper.js';
import { notifySignal } from '../src/notifications/dispatcher.js';
import type { Signal } from '../src/signals/types.js';
import type { AppLogger } from '../src/logger.js';

function testConfig(over: Partial<Record<string, unknown>> = {}): AppConfig {
  const base = {
    TELEGRAM_API_ID: 1,
    TELEGRAM_API_HASH: 'hash',
    TELEGRAM_PHONE_NUMBER: '+10000000000',
    TELEGRAM_SESSION_PATH: './state/session.txt',
    TELEGRAM_CHANNELS: ['@ch'],
    POLL_INTERVAL_SECONDS: 60,
    POLL_MAX_MESSAGES_PER_CHANNEL: 50,
    STATE_DIR: './state',
    LOGS_DIR: './logs',
    LOG_LEVEL: 'info',
    LOG_RAW_MESSAGES: false,
    PINDO_API_URL: 'https://api.pindo.io/v1/sms/',
    PINDO_TOKEN: 'token',
    PINDO_SENDER_ID: 'Co',
    NOTIFICATIONS_ENABLED: true,
    NOTIFICATION_NUMBERS: ['+250700000000'],
    HTTP_PORT: 8081,
    HTTP_HOST: '127.0.0.1',
    resolvedSessionPath: '/tmp/sess',
    resolvedStateDir: '/tmp/st',
    resolvedLogsDir: '/tmp/lg',
    resolvedCursorsPath: '/tmp/c.json',
    resolvedHandledPath: '/tmp/h.json',
  };
  return { ...base, ...over } as AppConfig;
}

const noopLogger = {
  warn: () => undefined,
  error: () => undefined,
  info: () => undefined,
  debug: () => undefined,
  child: () => noopLogger,
} as unknown as AppLogger;

const baseSignal: Signal = {
  id: 'sig-1',
  symbol: 'XAU',
  direction: 'BUY',
  sourceChannel: '@xauusdsignals',
  messageId: 10,
  messageDate: '2026-05-04T18:42:00.000Z',
  rawText: 'Gold buy now',
  channelId: '-1001',
};

describe('notifySignal', () => {
  let tmp: string;
  let smsPath: string;
  let errPath: string;
  let handledPath: string;

  beforeEach(async () => {
    tmp = await fs.mkdtemp(path.join(os.tmpdir(), 'disp-'));
    smsPath = path.join(tmp, 'sms.jsonl');
    errPath = path.join(tmp, 'errors.jsonl');
    handledPath = path.join(tmp, 'handled.json');
  });

  afterEach(async () => {
    await fs.rm(tmp, { recursive: true, force: true });
  });

  it('does not call Pindo when notifications disabled', async () => {
    const sendSms = vi.fn(async () => ({ ok: true as const, raw: {} }));
    const pindo = { sendSms };
    const smsLog = createJsonlWriter(smsPath);
    const errorsLog = createJsonlWriter(errPath);

    await notifySignal(baseSignal, {
      config: testConfig({ NOTIFICATIONS_ENABLED: false }),
      logger: noopLogger,
      pindo: pindo as never,
      smsLog,
      errorsLog,
      deduper: new Deduper(),
      handledPath,
    });
    await smsLog.flush();
    expect(sendSms).not.toHaveBeenCalled();
  });

  it('does not call Pindo when NOTIFICATION_NUMBERS empty', async () => {
    const sendSms = vi.fn(async () => ({ ok: true as const, raw: {} }));
    const pindo = { sendSms };
    const smsLog = createJsonlWriter(smsPath);
    const errorsLog = createJsonlWriter(errPath);

    await notifySignal(baseSignal, {
      config: testConfig({ NOTIFICATION_NUMBERS: [] }),
      logger: noopLogger,
      pindo: pindo as never,
      smsLog,
      errorsLog,
      deduper: new Deduper(),
      handledPath,
    });
    await smsLog.flush();
    expect(sendSms).not.toHaveBeenCalled();
  });

  it('logs ok=true on success', async () => {
    const pindo = {
      sendSms: vi.fn(async () => ({ ok: true as const, raw: { id: 1 } })),
    };
    const smsLog = createJsonlWriter(smsPath);
    const errorsLog = createJsonlWriter(errPath);

    await notifySignal(baseSignal, {
      config: testConfig(),
      logger: noopLogger,
      pindo: pindo as never,
      smsLog,
      errorsLog,
      deduper: new Deduper(),
      handledPath,
    });
    await smsLog.flush();
    const lines = (await fs.readFile(smsPath, 'utf8')).trim().split('\n');
    const row = JSON.parse(lines[0]!);
    expect(row.ok).toBe(true);
    expect(row.signalId).toBe('sig-1');
  });

  it('logs ok=false and errors on Pindo failure', async () => {
    const pindo = {
      sendSms: vi.fn(async () => ({ ok: false as const, status: 400, body: { err: 'bad' } })),
    };
    const smsLog = createJsonlWriter(smsPath);
    const errorsLog = createJsonlWriter(errPath);

    await notifySignal(baseSignal, {
      config: testConfig(),
      logger: noopLogger,
      pindo: pindo as never,
      smsLog,
      errorsLog,
      deduper: new Deduper(),
      handledPath,
    });
    await smsLog.flush();
    await errorsLog.flush();
    const smsLine = (await fs.readFile(smsPath, 'utf8')).trim().split('\n')[0]!;
    expect(JSON.parse(smsLine).ok).toBe(false);
    const errLine = (await fs.readFile(errPath, 'utf8')).trim().split('\n')[0]!;
    expect(JSON.parse(errLine).kind).toBe('sms_failed');
  });
});
