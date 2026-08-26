import { Module } from '@nestjs/common';
import { TypeOrmModule } from '@nestjs/typeorm';
import { ApiKeyGuard } from '../auth/api-key.guard';
import { ChannelsModule } from '../channels/channels.module';
import { NotificationDelivery } from '../database/entities/notification-delivery.entity';
import { NotificationRequest } from '../database/entities/notification-request.entity';
import { WhatsappWebhookController } from '../webhooks/whatsapp-webhook.controller';
import { DispatcherService } from './dispatcher.service';
import { NotificationsController } from './notifications.controller';
import { NotificationsService } from './notifications.service';

@Module({
  imports: [
    ChannelsModule,
    TypeOrmModule.forFeature([NotificationRequest, NotificationDelivery]),
  ],
  controllers: [NotificationsController, WhatsappWebhookController],
  providers: [NotificationsService, DispatcherService, ApiKeyGuard],
  exports: [DispatcherService],
})
export class NotificationsModule {}
