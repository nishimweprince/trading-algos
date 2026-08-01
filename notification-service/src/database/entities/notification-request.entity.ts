import {
  Column,
  CreateDateColumn,
  Entity,
  OneToMany,
  PrimaryGeneratedColumn,
} from 'typeorm';
import { NotificationDelivery } from './notification-delivery.entity';

@Entity('notification_requests')
export class NotificationRequest {
  @PrimaryGeneratedColumn('uuid')
  id!: string;

  @Column()
  source!: string;

  @Column({ type: 'text', nullable: true })
  subject?: string | null;

  @Column({ type: 'text' })
  message!: string;

  @Column({ default: 'text' })
  contentType!: string;

  @Column({ type: 'text' })
  channels!: string;

  @CreateDateColumn()
  createdAt!: Date;

  @OneToMany(() => NotificationDelivery, (delivery) => delivery.request)
  deliveries!: NotificationDelivery[];
}
