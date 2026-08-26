import { Injectable } from '@nestjs/common';
import { ConfigService as NestConfigService } from '@nestjs/config';
import {
  loadEnvConfig,
  NotificationChannel,
  ParsedConfig,
} from './env.schema';

@Injectable()
export class AppConfigService {
  private readonly config: ParsedConfig;

  constructor(nestConfig: NestConfigService) {
    this.config = loadEnvConfig({
      PORT: nestConfig.get<string>('PORT'),
      DATABASE_URL: nestConfig.get<string>('DATABASE_URL'),
      NOTIFICATIONS_ENABLED: nestConfig.get<string>('NOTIFICATIONS_ENABLED'),
      NOTIFICATION_API_KEY: nestConfig.get<string>('NOTIFICATION_API_KEY'),
      NOTIFICATION_PHONES: nestConfig.get<string>('NOTIFICATION_PHONES'),
      NOTIFICATION_EMAILS: nestConfig.get<string>('NOTIFICATION_EMAILS'),
      NOTIFICATION_TELEGRAM_CHAT_IDS: nestConfig.get<string>(
        'NOTIFICATION_TELEGRAM_CHAT_IDS',
      ),
      TG_BOT_TOKEN: nestConfig.get<string>('TG_BOT_TOKEN'),
      RESEND_API_KEY: nestConfig.get<string>('RESEND_API_KEY'),
      NOTIFICATION_FROM_EMAIL: nestConfig.get<string>('NOTIFICATION_FROM_EMAIL'),
      PINDO_API_URL: nestConfig.get<string>('PINDO_API_URL'),
      PINDO_TOKEN: nestConfig.get<string>('PINDO_TOKEN'),
      PINDO_SENDER_ID: nestConfig.get<string>('PINDO_SENDER_ID'),
      WHATSAPP_ACCESS_TOKEN: nestConfig.get<string>('WHATSAPP_ACCESS_TOKEN'),
      WHATSAPP_PHONE_NUMBER_ID: nestConfig.get<string>(
        'WHATSAPP_PHONE_NUMBER_ID',
      ),
      WHATSAPP_API_VERSION: nestConfig.get<string>('WHATSAPP_API_VERSION'),
      WHATSAPP_VERIFY_TOKEN: nestConfig.get<string>('WHATSAPP_VERIFY_TOKEN'),
      WHATSAPP_APP_SECRET: nestConfig.get<string>('WHATSAPP_APP_SECRET'),
    });
  }

  get port(): number {
    return this.config.PORT;
  }

  get databaseUrl(): string {
    return this.config.DATABASE_URL;
  }

  get databasePath(): string {
    const url = this.config.DATABASE_URL;
    if (url.startsWith('file:')) {
      return url.slice('file:'.length);
    }
    return url;
  }

  get notificationsEnabled(): boolean {
    return this.config.NOTIFICATIONS_ENABLED;
  }

  get apiKey(): string | undefined {
    const key = this.config.NOTIFICATION_API_KEY?.trim();
    return key || undefined;
  }

  get phones(): string[] {
    return this.config.notificationPhones;
  }

  get emails(): string[] {
    return this.config.notificationEmails;
  }

  get telegramChatIds(): string[] {
    return this.config.notificationTelegramChatIds;
  }

  get tgBotToken(): string | undefined {
    const token = this.config.TG_BOT_TOKEN?.trim();
    return token || undefined;
  }

  get resendApiKey(): string | undefined {
    const key = this.config.RESEND_API_KEY?.trim();
    return key || undefined;
  }

  get fromEmail(): string | undefined {
    const email = this.config.NOTIFICATION_FROM_EMAIL?.trim();
    return email || undefined;
  }

  get pindoApiUrl(): string {
    return this.config.PINDO_API_URL;
  }

  get pindoToken(): string | undefined {
    const token = this.config.PINDO_TOKEN?.trim();
    return token || undefined;
  }

  get pindoSenderId(): string {
    return this.config.PINDO_SENDER_ID;
  }

  get whatsappAccessToken(): string | undefined {
    const token = this.config.WHATSAPP_ACCESS_TOKEN?.trim();
    return token || undefined;
  }

  get whatsappPhoneNumberId(): string | undefined {
    const id = this.config.WHATSAPP_PHONE_NUMBER_ID?.trim();
    return id || undefined;
  }

  get whatsappApiVersion(): string {
    return this.config.WHATSAPP_API_VERSION;
  }

  get whatsappVerifyToken(): string | undefined {
    const token = this.config.WHATSAPP_VERIFY_TOKEN?.trim();
    return token || undefined;
  }

  get whatsappAppSecret(): string | undefined {
    const secret = this.config.WHATSAPP_APP_SECRET?.trim();
    return secret || undefined;
  }

  isTelegramConfigured(): boolean {
    return Boolean(this.tgBotToken);
  }

  isEmailConfigured(): boolean {
    return Boolean(this.resendApiKey && this.fromEmail);
  }

  isSmsConfigured(): boolean {
    return Boolean(this.pindoToken);
  }

  isWhatsappConfigured(): boolean {
    return Boolean(this.whatsappAccessToken && this.whatsappPhoneNumberId);
  }

  isChannelConfigured(channel: NotificationChannel): boolean {
    switch (channel) {
      case 'TELEGRAM':
        return this.isTelegramConfigured();
      case 'EMAIL':
        return this.isEmailConfigured();
      case 'SMS':
        return this.isSmsConfigured();
      case 'WHATSAPP':
        return this.isWhatsappConfigured();
    }
  }

  recipientsForChannel(channel: NotificationChannel): string[] {
    switch (channel) {
      case 'TELEGRAM':
        return this.telegramChatIds;
      case 'EMAIL':
        return this.emails;
      case 'SMS':
      case 'WHATSAPP':
        return this.phones;
    }
  }
}
