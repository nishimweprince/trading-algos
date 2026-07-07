import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ConfigSchema } from '../src/config/schema.ts';
import { TypedBus } from '../src/core/bus.ts';
import { Alerter } from '../src/alerts/telegram.ts';

const sendMessage = vi.hoisted(() => vi.fn());
const BotMock = vi.hoisted(() =>
  vi.fn().mockImplementation(() => ({
    api: { sendMessage },
  })),
);

vi.mock('grammy', () => ({ Bot: BotMock }));

const TOKEN_ENV = 'TEST_TG_BOT_TOKEN';

beforeEach(() => {
  process.env[TOKEN_ENV] = 'telegram-token';
  sendMessage.mockReset();
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

function makeConfig() {
  return ConfigSchema.parse({
    alerts: {
      telegramBotTokenEnvVar: TOKEN_ENV,
      chatId: 12345,
    },
  });
}
