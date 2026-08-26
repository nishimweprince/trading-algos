import { Injectable, Logger } from '@nestjs/common';
import axios from 'axios';
import { AppConfigService } from '../config/config.service';
import {
  ChannelSendParams,
  ChannelSendResult,
  ChannelSender,
} from './channel.interface';
import { stripHtml } from './message.util';

@Injectable()
export class EmailChannel implements ChannelSender {
  readonly channel = 'EMAIL' as const;
  private readonly logger = new Logger(EmailChannel.name);

  constructor(private readonly config: AppConfigService) {}

  isConfigured(): boolean {
    return this.config.isEmailConfigured();
  }

  async send(params: ChannelSendParams): Promise<ChannelSendResult> {
    const apiKey = this.config.resendApiKey;
    const from = this.config.fromEmail;
    if (!apiKey || !from) {
      throw new Error('Email not configured');
    }

    const subject =
      params.subject?.trim() || `[${params.source}] notification`;

    const payload: Record<string, unknown> = {
      from,
      to: [params.recipient],
      subject,
    };

    if (params.contentType === 'html') {
      payload.html = params.message;
      payload.text = stripHtml(params.message);
    } else {
      payload.text = params.message;
    }

    const response = await axios.post('https://api.resend.com/emails', payload, {
      headers: {
        Authorization: `Bearer ${apiKey}`,
        'Content-Type': 'application/json',
      },
      timeout: 30_000,
      validateStatus: () => true,
    });

    if (response.status >= 400) {
      const detail =
        typeof response.data === 'object'
          ? JSON.stringify(response.data)
          : String(response.data);
      throw new Error(`Resend API returned ${response.status}: ${detail}`);
    }

    const externalId =
      response.data && typeof response.data === 'object' && 'id' in response.data
        ? String((response.data as { id: string }).id)
        : undefined;

    this.logger.log(`Email sent to ${params.recipient}`);
    return { externalId };
  }
}
