import { Injectable, Logger } from '@nestjs/common';
import axios, { AxiosInstance } from 'axios';
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
export class SmsChannel implements ChannelSender {
  readonly channel = 'SMS' as const;
  private readonly logger = new Logger(SmsChannel.name);
  private http: AxiosInstance | null = null;

  constructor(private readonly config: AppConfigService) {}

  isConfigured(): boolean {
    return this.config.isSmsConfigured();
  }

  private getHttp(): AxiosInstance {
    if (!this.http) {
      this.http = axios.create({
        baseURL: this.config.pindoApiUrl.replace(/\/?$/, '/'),
        timeout: 30_000,
        headers: { 'Content-Type': 'application/json' },
        validateStatus: () => true,
      });
    }
    return this.http;
  }

  async send(params: ChannelSendParams): Promise<ChannelSendResult> {
    const token = this.config.pindoToken;
    if (!token) {
      throw new Error('SMS not configured');
    }

    const text = stripHtml(
      formatMessageWithSubject(params.subject, params.message),
    ).slice(0, 150);

    const response = await this.getHttp().post(
      '',
      {
        to: params.recipient,
        text,
        sender: this.config.pindoSenderId,
      },
      { headers: { Authorization: `Bearer ${token}` } },
    );

    if (response.status < 200 || response.status >= 300) {
      const detail =
        typeof response.data === 'object'
          ? JSON.stringify(response.data)
          : String(response.data);
      throw new Error(`Pindo API returned ${response.status}: ${detail}`);
    }

    this.logger.log(`SMS sent to ${params.recipient}`);
    return {};
  }
}
