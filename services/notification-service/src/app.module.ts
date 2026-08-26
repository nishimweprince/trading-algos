import { Module } from '@nestjs/common';
import { AppConfigModule } from './config/config.module';
import { DatabaseModule } from './database/database.module';
import { NotificationsModule } from './notifications/notifications.module';

@Module({
  imports: [AppConfigModule, DatabaseModule, NotificationsModule],
})
export class AppModule {}
