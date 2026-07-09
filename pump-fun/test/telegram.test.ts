import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ConfigSchema } from '../src/config/schema.ts';
import { TypedBus } from '../src/core/bus.ts';
import { Alerter } from '../src/alerts/telegram.ts';

const sendMessage = vi.hoisted(() => vi.fn());
const commandHandlers = vi.hoisted(() => new Map<string, (ctx: { from?: { id: number }; reply: (text: string) => Promise<void> }) => Promise<void>>());
const start = vi.hoisted(() => vi.fn(async () => undefined));
const stop = vi.hoisted(() => vi.fn(async () => undefined));
const BotMock = vi.hoisted(() =>
  vi.fn().mockImplementation(() => ({
    api: { sendMessage },
    command: vi.fn((name: string, handler: (ctx: { from?: { id: number }; reply: (text: string) => Promise<void> }) => Promise<void>) => {
      commandHandlers.set(name, handler);
    }),
    start,
    stop,
  })),
);

vi.mock('grammy', () => ({ Bot: BotMock }));

const TOKEN_ENV = 'TEST_TG_BOT_TOKEN';

beforeEach(() => {
  process.env[TOKEN_ENV] = 'telegram-token';
  sendMessage.mockReset();
  commandHandlers.clear();
  start.mockClear();
  stop.mockClear();
  BotMock.mockClear();
});

afterEach(() => {
  delete process.env[TOKEN_ENV];
});

describe('Alerter Telegram filtering', () => {
  it('does not send dashboard-only alerts to Telegram', () => {
    const bus = new TypedBus();
    const alerter = Alerter.create(makeConfig());
    const detach = alerter.attach(bus);

    bus.emit('alert', { level: 'info', message: 'dashboard only' });

    expect(sendMessage).not.toHaveBeenCalled();
    detach();
  });

  it('sends alerts that explicitly opt in to Telegram', () => {
    const bus = new TypedBus();
    const alerter = Alerter.create(makeConfig());
    const detach = alerter.attach(bus);

    bus.emit('alert', { level: 'info', message: 'position opened', telegram: true });

    expect(sendMessage).toHaveBeenCalledWith(12345, 'ℹ️ position opened', { parse_mode: 'HTML' });
    detach();
  });

  it('does not throw when a Telegram send fails', async () => {
    const bus = new TypedBus();
    const alerter = Alerter.create(makeConfig());
    const detach = alerter.attach(bus);
    sendMessage.mockRejectedValueOnce(new Error('network down'));

    expect(() => bus.emit('alert', { level: 'warn', message: 'position exit', telegram: true })).not.toThrow();
    await vi.waitFor(() => expect(sendMessage).toHaveBeenCalledTimes(1));

    detach();
  });
});

describe('Alerter admin commands', () => {
  it('gates /kill to admins and emits the kill switch only for an admin', async () => {
    const bus = new TypedBus();
    const alerter = Alerter.create(makeConfig({ adminUserIds: [42] }));
    const kills: string[] = [];
    bus.on('killSwitch', (e) => kills.push(`${e.source}:${e.detail ?? ''}`));
    alerter.startCommands({ bus, adminUserIds: [42], getStatus: () => 'ok' });
    const kill = commandHandlers.get('kill');
    expect(kill).toBeDefined();
    expect(start).toHaveBeenCalledWith({ drop_pending_updates: true });

    const outsiderReply = vi.fn(async () => undefined);
    await kill!({ from: { id: 7 }, reply: outsiderReply });
    expect(kills).toEqual([]);
    expect(outsiderReply).not.toHaveBeenCalled();

    const adminReply = vi.fn(async () => undefined);
    await kill!({ from: { id: 42 }, reply: adminReply });
    expect(kills).toEqual(['telegram:user 42']);
    expect(adminReply).toHaveBeenCalledWith('🛑 kill switch engaged — flattening positions, entries halted.');

    await alerter.stop();
    expect(stop).toHaveBeenCalledOnce();
  });

  it('gates /status to admins and replies with the supplied status', async () => {
    const bus = new TypedBus();
    const alerter = Alerter.create(makeConfig({ adminUserIds: [42] }));
    alerter.startCommands({ bus, adminUserIds: [42], getStatus: () => 'risk ok | open 0' });
    const status = commandHandlers.get('status');
    expect(status).toBeDefined();

    const outsiderReply = vi.fn(async () => undefined);
    await status!({ from: { id: 7 }, reply: outsiderReply });
    expect(outsiderReply).not.toHaveBeenCalled();

    const adminReply = vi.fn(async () => undefined);
    await status!({ from: { id: 42 }, reply: adminReply });
    expect(adminReply).toHaveBeenCalledWith('risk ok | open 0');

    await alerter.stop();
  });
});

function makeConfig(alerts: { adminUserIds?: number[] } = {}) {
  return ConfigSchema.parse({
    alerts: {
      telegramBotTokenEnvVar: TOKEN_ENV,
      chatId: 12345,
      ...alerts,
    },
  });
}
