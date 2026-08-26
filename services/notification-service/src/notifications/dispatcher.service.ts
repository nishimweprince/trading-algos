import {
  BadRequestException,
  Injectable,
  Logger,
  ServiceUnavailableException,
} from '@nestjs/common';
import { InjectRepository } from '@nestjs/typeorm';
import { QueryFailedError, Repository } from 'typeorm';
import { AppConfigService } from '../config/config.service';
import { NotificationChannel } from '../config/env.schema';
import { EmailChannel } from '../channels/email.channel';
import { ChannelSender } from '../channels/channel.interface';
import { SmsChannel } from '../channels/sms.channel';
import { TelegramChannel } from '../channels/telegram.channel';
import { WhatsappChannel } from '../channels/whatsapp.channel';
import { NotificationDelivery } from '../database/entities/notification-delivery.entity';
import { NotificationRequest } from '../database/entities/notification-request.entity';
import { DeliveryChannel, DeliveryStatus } from '../database/enums';
import { SendNotificationDto } from './dto/send-notification.dto';

export interface SendNotificationResult {
  requestId: string;
  deliveryIds: string[];
  deliveriesAttempted: number;
  skipped?: boolean;
  reason?: string;
  deduplicated?: boolean;
}

@Injectable()
export class DispatcherService {
  private readonly logger = new Logger(DispatcherService.name);
  private readonly channelMap: Map<NotificationChannel, ChannelSender>;

  constructor(
    @InjectRepository(NotificationRequest)
    private readonly requestRepo: Repository<NotificationRequest>,
    @InjectRepository(NotificationDelivery)
    private readonly deliveryRepo: Repository<NotificationDelivery>,
    private readonly config: AppConfigService,
    telegram: TelegramChannel,
    email: EmailChannel,
    sms: SmsChannel,
    whatsapp: WhatsappChannel,
  ) {
    this.channelMap = new Map<NotificationChannel, ChannelSender>([
      ['TELEGRAM', telegram],
      ['EMAIL', email],
      ['SMS', sms],
      ['WHATSAPP', whatsapp],
    ]);
  }

  async send(dto: SendNotificationDto): Promise<SendNotificationResult> {
    const existing = await this.findIdempotentRequest(dto);
    if (existing) return existing;

    if (!this.config.notificationsEnabled) {
      const created = await this.createRequest(dto);
      if (created.deduplicated) return this.resultFor(created.request, true);
      const request = created.request;
      return {
        requestId: request.id,
        deliveryIds: [],
        deliveriesAttempted: 0,
        skipped: true,
        reason: 'Notifications disabled',
      };
    }

    const channels = dto.channels as NotificationChannel[];
    const sendable = channels.filter((ch) => this.isChannelSendable(ch));

    if (sendable.length === 0) {
      throw new ServiceUnavailableException(
        'No requested notification channels are configured',
      );
    }

    for (const channel of channels) {
      if (!sendable.includes(channel)) continue;
      const recipients = this.config.recipientsForChannel(channel);
      if (recipients.length === 0) {
        throw new BadRequestException(
          `No recipients configured for channel ${channel}`,
        );
      }
    }

    const created = await this.createRequest(dto);
    if (created.deduplicated) return this.resultFor(created.request, true);
    const request = created.request;

    const deliveryPlans: Array<{
      channel: NotificationChannel;
      recipient: string;
    }> = [];

    for (const channel of sendable) {
      for (const recipient of this.config.recipientsForChannel(channel)) {
        deliveryPlans.push({ channel, recipient });
      }
    }

    const deliveries = await Promise.all(
      deliveryPlans.map((plan) =>
        this.deliveryRepo.save(
          this.deliveryRepo.create({
            requestId: request.id,
            channel: plan.channel as DeliveryChannel,
            recipient: plan.recipient,
            status: DeliveryStatus.pending,
          }),
        ),
      ),
    );

    const results = await Promise.allSettled(
      deliveries.map((delivery, index) =>
        this.deliverOne(delivery.id, deliveryPlans[index], dto),
      ),
    );

    for (const result of results) {
      if (result.status === 'rejected') {
        this.logger.error(`Unexpected delivery failure: ${result.reason}`);
      }
    }

    return {
      requestId: request.id,
      deliveryIds: deliveries.map((d) => d.id),
      deliveriesAttempted: deliveries.length,
    };
  }

  private async findIdempotentRequest(
    dto: SendNotificationDto,
  ): Promise<SendNotificationResult | null> {
    if (!dto.idempotencyKey) return null;
    const request = await this.requestRepo.findOne({
      where: {
        source: dto.source,
        idempotencyKey: dto.idempotencyKey,
      },
      relations: { deliveries: true },
    });
    if (!request) return null;
    return this.resultFor(request, true);
  }

  private resultFor(
    request: NotificationRequest,
    deduplicated: boolean,
  ): SendNotificationResult {
    const deliveries = request.deliveries ?? [];
    return {
      requestId: request.id,
      deliveryIds: deliveries.map((delivery) => delivery.id),
      deliveriesAttempted: deliveries.length,
      deduplicated,
      ...(deliveries.length === 0
        ? { skipped: true, reason: 'Original request created no deliveries' }
        : {}),
    };
  }

  private async createRequest(
    dto: SendNotificationDto,
  ): Promise<{ request: NotificationRequest; deduplicated: boolean }> {
    try {
      const request = await this.requestRepo.save(
        this.requestRepo.create({
          source: dto.source,
          idempotencyKey: dto.idempotencyKey ?? null,
          subject: dto.subject,
          message: dto.message,
          contentType: dto.contentType,
          channels: JSON.stringify(dto.channels),
        }),
      );
      return { request, deduplicated: false };
    } catch (error) {
      // The preflight lookup handles normal retries. The unique constraint and
      // this recovery path close the race where two requests arrive together.
      if (dto.idempotencyKey && error instanceof QueryFailedError) {
        const request = await this.requestRepo.findOne({
          where: { source: dto.source, idempotencyKey: dto.idempotencyKey },
          relations: { deliveries: true },
        });
        if (request) return { request, deduplicated: true };
      }
      throw error;
    }
  }

  private isChannelSendable(channel: NotificationChannel): boolean {
    const sender = this.channelMap.get(channel);
    return Boolean(sender?.isConfigured());
  }

  private async deliverOne(
    deliveryId: string,
    plan: { channel: NotificationChannel; recipient: string },
    dto: SendNotificationDto,
  ): Promise<void> {
    const sender = this.channelMap.get(plan.channel);
    if (!sender) {
      await this.markFailed(deliveryId, 'Channel sender not found');
      return;
    }

    try {
      const result = await sender.send({
        recipient: plan.recipient,
        subject: dto.subject,
        message: dto.message,
        contentType: dto.contentType as 'text' | 'html',
        source: dto.source,
      });

      await this.deliveryRepo.update(deliveryId, {
        status: DeliveryStatus.sent,
        externalId: result.externalId ?? null,
        sentAt: new Date(),
        error: null,
      });
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      this.logger.error(
        `Delivery ${deliveryId} failed (${plan.channel} → ${plan.recipient}): ${message}`,
      );
      await this.markFailed(deliveryId, message);
    }
  }

  private async markFailed(deliveryId: string, error: string): Promise<void> {
    await this.deliveryRepo.update(deliveryId, {
      status: DeliveryStatus.failed,
      error,
    });
  }

  async updateStatusByExternalId(
    externalId: string,
    status: string,
    error?: string,
  ): Promise<number> {
    const mapped = this.mapWebhookStatus(status);
    if (!mapped) return 0;

    const result = await this.deliveryRepo.update(
      { externalId },
      {
        status: mapped,
        error: error ?? null,
      },
    );
    return result.affected ?? 0;
  }

  private mapWebhookStatus(status: string): DeliveryStatus | null {
    switch (status.toLowerCase()) {
      case 'sent':
        return DeliveryStatus.sent;
      case 'delivered':
        return DeliveryStatus.delivered;
      case 'read':
        return DeliveryStatus.read;
      case 'failed':
        return DeliveryStatus.failed;
      default:
        return null;
    }
  }
}
