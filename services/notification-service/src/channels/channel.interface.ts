import { NotificationChannel } from '../config/env.schema';

export type ContentType = 'text' | 'html';

export interface ChannelSendParams {
  recipient: string;
  subject?: string;
  message: string;
  contentType: ContentType;
  source: string;
}

export interface ChannelSendResult {
  externalId?: string;
}

export interface ChannelSender {
  readonly channel: NotificationChannel;
  isConfigured(): boolean;
  send(params: ChannelSendParams): Promise<ChannelSendResult>;
}
