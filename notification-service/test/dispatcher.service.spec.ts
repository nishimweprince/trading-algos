import {
  BadRequestException,
  ServiceUnavailableException,
} from '@nestjs/common';
import { DispatcherService } from '../src/notifications/dispatcher.service';
import { AppConfigService } from '../src/config/config.service';
import { ChannelSender } from '../src/channels/channel.interface';
import { DeliveryStatus } from '../src/database/enums';

function createMockSender(
  channel: 'TELEGRAM' | 'EMAIL' | 'SMS' | 'WHATSAPP',
  configured = true,
  fail = false,
): ChannelSender {
  return {
    channel,
    isConfigured: () => configured,
    send: async () => {
      if (fail) throw new Error('send failed');
      return { externalId: 'ext-123' };
    },
  };
}

function createMockRepo() {
  const deliveryUpdates: Array<Record<string, unknown>> = [];

  const requestRepo = {
    create: jest.fn((data) => data),
    save: jest.fn().mockImplementation((data) =>
      Promise.resolve({ id: 'req-1', ...data }),
    ),
  };

  const deliveryRepo = {
    create: jest.fn((data) => data),
    save: jest.fn().mockImplementation((data) =>
      Promise.resolve({ id: `del-${data.channel}`, ...data }),
    ),
    update: jest.fn().mockImplementation((_id, data) => {
      deliveryUpdates.push(data);
      return Promise.resolve({ affected: 1 });
    }),
  };

  return { requestRepo, deliveryRepo, deliveryUpdates };
}

function createDispatcher(overrides: {
  config?: Partial<AppConfigService>;
  repos?: ReturnType<typeof createMockRepo>;
  senders?: {
    telegram?: ChannelSender;
    email?: ChannelSender;
    sms?: ChannelSender;
    whatsapp?: ChannelSender;
  };
}): DispatcherService & {
  deliveryUpdates: Array<Record<string, unknown>>;
} {
  const config = {
    notificationsEnabled: true,
    recipientsForChannel: (ch: string) => {
      if (ch === 'EMAIL') return ['a@example.com'];
      if (ch === 'TELEGRAM') return ['12345'];
      return ['+250700000000'];
    },
    ...overrides.config,
  } as AppConfigService;

  const repos = overrides.repos ?? createMockRepo();

  const telegram =
    overrides.senders?.telegram ?? createMockSender('TELEGRAM');
  const email = overrides.senders?.email ?? createMockSender('EMAIL');
  const sms = overrides.senders?.sms ?? createMockSender('SMS');
  const whatsapp =
    overrides.senders?.whatsapp ?? createMockSender('WHATSAPP');

  const dispatcher = new DispatcherService(
    repos.requestRepo as never,
    repos.deliveryRepo as never,
    config,
    telegram as never,
    email as never,
    sms as never,
    whatsapp as never,
  );

  return Object.assign(dispatcher, {
    deliveryUpdates: repos.deliveryUpdates,
  });
}

describe('DispatcherService', () => {
  it('fans out to configured channels and recipients', async () => {
    const dispatcher = createDispatcher({});
    const result = await dispatcher.send({
      message: 'Hello',
      contentType: 'text' as never,
      channels: ['EMAIL', 'SMS'] as never,
      source: 'test',
    });

    expect(result.deliveriesAttempted).toBe(2);
    expect(result.deliveryIds).toHaveLength(2);
  });

  it('throws when no channels are configured', async () => {
    const dispatcher = createDispatcher({
      senders: {
        email: createMockSender('EMAIL', false),
        sms: createMockSender('SMS', false),
      },
    });

    await expect(
      dispatcher.send({
        message: 'Hello',
        contentType: 'text' as never,
        channels: ['EMAIL'] as never,
        source: 'test',
      }),
    ).rejects.toThrow(ServiceUnavailableException);
  });

  it('throws when recipients are empty for a channel', async () => {
    const dispatcher = createDispatcher({
      config: {
        recipientsForChannel: () => [],
      } as never,
    });

    await expect(
      dispatcher.send({
        message: 'Hello',
        contentType: 'text' as never,
        channels: ['EMAIL'] as never,
        source: 'test',
      }),
    ).rejects.toThrow(BadRequestException);
  });

  it('marks failed deliveries without blocking others', async () => {
    const dispatcher = createDispatcher({
      senders: {
        email: createMockSender('EMAIL', true, true),
        sms: createMockSender('SMS', true, false),
      },
    });

    await dispatcher.send({
      message: 'Hello',
      contentType: 'text' as never,
      channels: ['EMAIL', 'SMS'] as never,
      source: 'test',
    });

    const statuses = dispatcher.deliveryUpdates.map((u) => u.status);
    expect(statuses).toContain(DeliveryStatus.failed);
    expect(statuses).toContain(DeliveryStatus.sent);
  });

  it('returns skipped result when notifications are disabled', async () => {
    const dispatcher = createDispatcher({
      config: { notificationsEnabled: false } as never,
    });

    const result = await dispatcher.send({
      message: 'Hello',
      contentType: 'text' as never,
      channels: ['EMAIL'] as never,
      source: 'test',
    });

    expect(result.skipped).toBe(true);
    expect(result.deliveriesAttempted).toBe(0);
  });

  it('updates status by external id from webhook', async () => {
    const repos = createMockRepo();
    const dispatcher = createDispatcher({ repos });

    const count = await dispatcher.updateStatusByExternalId(
      'wamid-1',
      'delivered',
    );
    expect(count).toBe(1);
    expect(repos.deliveryRepo.update).toHaveBeenCalledWith(
      { externalId: 'wamid-1' },
      expect.objectContaining({ status: DeliveryStatus.delivered }),
    );
  });
});
