import { Module } from '@nestjs/common';
import { EmailChannel } from './email.channel';
import { SmsChannel } from './sms.channel';
import { TelegramChannel } from './telegram.channel';
import { WhatsappChannel } from './whatsapp.channel';

@Module({
  providers: [TelegramChannel, EmailChannel, SmsChannel, WhatsappChannel],
  exports: [TelegramChannel, EmailChannel, SmsChannel, WhatsappChannel],
})
export class ChannelsModule {}
