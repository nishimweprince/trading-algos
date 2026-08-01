import {
  Column,
  CreateDateColumn,
  Entity,
  Index,
  JoinColumn,
  ManyToOne,
  PrimaryGeneratedColumn,
  UpdateDateColumn,
} from 'typeorm';
import { DeliveryChannel, DeliveryStatus } from '../enums';
import { NotificationRequest } from './notification-request.entity';

@Entity('notification_deliveries')
@Index(['requestId'])
@Index(['status'])
@Index(['channel'])
@Index(['externalId'])
export class NotificationDelivery {
  @PrimaryGeneratedColumn('uuid')
  id!: string;

  @Column()
  requestId!: string;

  @Column({ type: 'text' })
  channel!: DeliveryChannel;

  @Column()
  recipient!: string;

  @Column({ type: 'text', default: DeliveryStatus.pending })
  status!: DeliveryStatus;

  @Column({ type: 'text', nullable: true })
  externalId?: string | null;

  @Column({ type: 'text', nullable: true })
  error?: string | null;

  @CreateDateColumn()
  createdAt!: Date;

  @Column({ type: 'datetime', nullable: true })
  sentAt?: Date | null;

  @UpdateDateColumn()
  updatedAt!: Date;

  @ManyToOne(() => NotificationRequest, (request) => request.deliveries, {
    onDelete: 'CASCADE',
  })
  @JoinColumn({ name: 'requestId' })
  request!: NotificationRequest;
}
