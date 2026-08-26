import { Injectable, Logger } from '@nestjs/common';
import { Bot } from 'grammy';
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
export class TelegramChannel implements ChannelSender {
  readonly channel = 'TELEGRAM' as const;
  private readonly logger = new Logger(TelegramChannel.name);
  private bot: Bot | null = null;

  constructor(private readonly config: AppConfigService) {}

  isConfigured(): boolean {
    return this.config.isTelegramConfigured();
  }

  private getBot(): Bot {
    if (!this.bot) {
      const token = this.config.tgBotToken;
      if (!token) {
        throw new Error('Telegram not configured');
      }
      this.bot = new Bot(token);
    }
    return this.bot;
  }

  async send(params: ChannelSendParams): Promise<ChannelSendResult> {
    const bot = this.getBot();
    const text =
      params.contentType === 'html'
        ? formatMessageWithSubject(params.subject, params.message)
        : formatMessageWithSubject(
            params.subject,
            stripHtml(params.message),
          );

    const options =
      params.contentType === 'html'
        ? { parse_mode: 'HTML' as const }
        : undefined;

    const result = await bot.api.sendMessage(params.recipient, text, options);
    this.logger.log(`Telegram message sent to ${params.recipient}`);
    return { externalId: String(result.message_id) };
  }
}
