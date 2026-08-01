import { Injectable, NotFoundException } from '@nestjs/common';
import { InjectRepository } from '@nestjs/typeorm';
import { Repository } from 'typeorm';
import { NotificationDelivery } from '../database/entities/notification-delivery.entity';
import { DeliveryStatus } from '../database/enums';
import { ListNotificationsQueryDto } from './dto/list-notifications.query';
import { SendNotificationDto } from './dto/send-notification.dto';
import { DispatcherService, SendNotificationResult } from './dispatcher.service';

@Injectable()
export class NotificationsService {
  constructor(
    @InjectRepository(NotificationDelivery)
    private readonly deliveryRepo: Repository<NotificationDelivery>,
    private readonly dispatcher: DispatcherService,
  ) {}

  send(dto: SendNotificationDto): Promise<SendNotificationResult> {
    return this.dispatcher.send(dto);
  }

  async list(query: ListNotificationsQueryDto) {
    const qb = this.deliveryRepo
      .createQueryBuilder('delivery')
      .innerJoinAndSelect('delivery.request', 'request')
      .orderBy('delivery.createdAt', 'DESC')
      .take(query.limit ?? 100);

    if (query.recipient) {
      qb.andWhere('delivery.recipient = :recipient', {
        recipient: query.recipient,
      });
    }
    if (query.status) {
      qb.andWhere('delivery.status = :status', {
        status: query.status as DeliveryStatus,
      });
    }
    if (query.channel) {
      qb.andWhere('delivery.channel = :channel', {
        channel: query.channel,
      });
    }
    if (query.source) {
      qb.andWhere('request.source = :source', { source: query.source });
    }

    const deliveries = await qb.getMany();
    return deliveries.map((d) => this.toDeliveryDict(d));
  }

  async getById(id: string) {
    const delivery = await this.deliveryRepo.findOne({
      where: { id },
      relations: { request: true },
    });

    if (!delivery) {
      throw new NotFoundException('Notification not found');
    }

    return this.toDeliveryDict(delivery);
  }

  private toDeliveryDict(delivery: NotificationDelivery) {
    return {
      id: delivery.id,
      requestId: delivery.requestId,
      source: delivery.request.source,
      subject: delivery.request.subject,
      message: delivery.request.message,
      contentType: delivery.request.contentType,
      channels: JSON.parse(delivery.request.channels) as string[],
      recipient: delivery.recipient,
      channel: delivery.channel,
      status: delivery.status,
      externalId: delivery.externalId,
      error: delivery.error,
      createdAt: delivery.createdAt.toISOString(),
      sentAt: delivery.sentAt?.toISOString() ?? null,
      updatedAt: delivery.updatedAt.toISOString(),
    };
  }
}
