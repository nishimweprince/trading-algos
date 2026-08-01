import {
  Body,
  Controller,
  Get,
  HttpStatus,
  Param,
  Post,
  Query,
  Res,
  UseGuards,
} from '@nestjs/common';
import { Response } from 'express';
import { ApiKeyGuard } from '../auth/api-key.guard';
import { ListNotificationsQueryDto } from './dto/list-notifications.query';
import { SendNotificationDto } from './dto/send-notification.dto';
import { NotificationsService } from './notifications.service';

@Controller('notifications')
@UseGuards(ApiKeyGuard)
export class NotificationsController {
  constructor(private readonly notifications: NotificationsService) {}

  @Post()
  async send(
    @Body() dto: SendNotificationDto,
    @Res({ passthrough: true }) res: Response,
  ) {
    const result = await this.notifications.send(dto);
    if (result.skipped) {
      res.status(HttpStatus.OK);
      return {
        ...result,
        status: 'skipped',
      };
    }
    res.status(HttpStatus.CREATED);
    return result;
  }

  @Get()
  list(@Query() query: ListNotificationsQueryDto) {
    return this.notifications.list(query);
  }

  @Get(':id')
  getById(@Param('id') id: string) {
    return this.notifications.getById(id);
  }
}
