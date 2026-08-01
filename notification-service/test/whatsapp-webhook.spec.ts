import { createHmac } from 'crypto';
import { WhatsappChannel } from '../src/channels/whatsapp.channel';
import { AppConfigService } from '../src/config/config.service';

function mockWhatsappConfig(
  overrides: Partial<AppConfigService> = {},
): AppConfigService {
  return {
    isWhatsappConfigured: () => true,
    whatsappVerifyToken: 'verify-token',
    whatsappAppSecret: 'app-secret',
    whatsappAccessToken: 'token',
    whatsappPhoneNumberId: '123',
    whatsappApiVersion: 'v21.0',
    ...overrides,
  } as AppConfigService;
}

describe('WhatsappChannel webhook helpers', () => {
  const channel = new WhatsappChannel(mockWhatsappConfig());

  it('verifies webhook challenge', () => {
    expect(
      channel.verifyWebhookChallenge('subscribe', 'verify-token', 'challenge'),
    ).toBe('challenge');
  });

  it('rejects invalid verify token', () => {
    expect(
      channel.verifyWebhookChallenge('subscribe', 'wrong', 'challenge'),
    ).toBeNull();
  });

  it('validates signature', () => {
    const body = Buffer.from('{"test":true}');
    const sig =
      'sha256=' +
      createHmac('sha256', 'app-secret').update(body).digest('hex');
    expect(channel.verifySignature(body, sig)).toBe(true);
  });

  it('rejects invalid signature', () => {
    const body = Buffer.from('{"test":true}');
    expect(channel.verifySignature(body, 'sha256=deadbeef')).toBe(false);
  });
});
