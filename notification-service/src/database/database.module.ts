import { Module } from '@nestjs/common';
import { TypeOrmModule } from '@nestjs/typeorm';
import { AppConfigModule } from '../config/config.module';
import { AppConfigService } from '../config/config.service';
import { NotificationDelivery } from './entities/notification-delivery.entity';
import { NotificationRequest } from './entities/notification-request.entity';

@Module({
  imports: [
    TypeOrmModule.forRootAsync({
      imports: [AppConfigModule],
      inject: [AppConfigService],
      useFactory: (config: AppConfigService) => ({
        type: 'better-sqlite3',
        database: config.databasePath,
        entities: [NotificationRequest, NotificationDelivery],
        synchronize: true,
      }),
    }),
    TypeOrmModule.forFeature([NotificationRequest, NotificationDelivery]),
  ],
  exports: [TypeOrmModule],
})
export class DatabaseModule {}
