import { Injectable, Logger } from '@nestjs/common';
import axios, { AxiosInstance } from 'axios';
import { createHmac, timingSafeEqual } from 'crypto';
import { AppConfigService } from '../config/config.service';
import {
  ChannelSendParams,
  ChannelSendResult,
  ChannelSender,
} from './channel.interface';
import {
  formatMessageWithSubject,
  stripHtml,
} from './message.util';

@Injectable()
export class WhatsappChannel implements ChannelSender {
  readonly channel = 'WHATSAPP' as const;
  private readonly logger = new Logger(WhatsappChannel.name);
  private http: AxiosInstance | null = null;

  constructor(private readonly config: AppConfigService) {}

  isConfigured(): boolean {
    return this.config.isWhatsappConfigured();
  }

  private getHttp(): AxiosInstance {
    if (!this.http) {
      this.http = axios.create({ timeout: 30_000, validateStatus: () => true });
    }
    return this.http;
  }

  private get sendUrl(): string {
    const version = this.config.whatsappApiVersion;
    const phoneId = this.config.whatsappPhoneNumberId;
    return `https://graph.facebook.com/${version}/${phoneId}/messages`;
  }

  async send(params: ChannelSendParams): Promise<ChannelSendResult> {
    const token = this.config.whatsappAccessToken;
    if (!token || !this.config.whatsappPhoneNumberId) {
      throw new Error('WhatsApp not configured');
    }

    const body = stripHtml(
      formatMessageWithSubject(params.subject, params.message),
    );

    const payload = {
      messaging_product: 'whatsapp',
      recipient_type: 'individual',
      to: params.recipient,
      type: 'text',
      text: { preview_url: false, body },
    };

    const response = await this.getHttp().post(this.sendUrl, payload, {
      headers: {
        Authorization: `Bearer ${token}`,
        'Content-Type': 'application/json',
      },
    });

    if (response.status >= 400) {
      const detail =
        typeof response.data === 'object'
          ? JSON.stringify(response.data)
          : String(response.data);
      throw new Error(`WhatsApp API returned ${response.status}: ${detail}`);
    }

    const messages = (response.data as { messages?: Array<{ id?: string }> })
      ?.messages;
    const externalId = messages?.[0]?.id;

    this.logger.log(`WhatsApp message sent to ${params.recipient}`);
    return { externalId };
  }

  verifyWebhookChallenge(
    mode: string | undefined,
    token: string | undefined,
    challenge: string | undefined,
  ): string | null {
    const expected = this.config.whatsappVerifyToken;
    if (!expected) {
      this.logger.warn('WHATSAPP_VERIFY_TOKEN not set; refusing verification');
      return null;
    }
    if (mode === 'subscribe' && token === expected && challenge) {
      return challenge;
    }
    this.logger.warn(`Webhook verification failed: mode=${mode}`);
    return null;
  }

  verifySignature(
    rawBody: Buffer,
    signatureHeader: string | undefined,
  ): boolean {
    const secret = this.config.whatsappAppSecret;
    if (!secret) {
      this.logger.warn('WHATSAPP_APP_SECRET not set; signature verification disabled');
      return false;
    }
    if (!signatureHeader?.startsWith('sha256=')) {
      return false;
    }

    const provided = signatureHeader.split('=', 2)[1];
    const expected = createHmac('sha256', secret)
      .update(rawBody)
      .digest('hex');

    try {
      return timingSafeEqual(
        Buffer.from(provided, 'utf8'),
        Buffer.from(expected, 'utf8'),
      );
    } catch {
      return false;
    }
  }
}
