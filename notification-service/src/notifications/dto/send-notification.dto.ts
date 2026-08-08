import {
  ArrayMinSize,
  IsArray,
  IsEnum,
  IsNotEmpty,
  IsOptional,
  IsString,
  MaxLength,
  MinLength,
} from 'class-validator';

export enum NotificationChannelDto {
  TELEGRAM = 'TELEGRAM',
  EMAIL = 'EMAIL',
  SMS = 'SMS',
  WHATSAPP = 'WHATSAPP',
}

export enum ContentTypeDto {
  text = 'text',
  html = 'html',
}

export class SendNotificationDto {
  @IsOptional()
  @IsString()
  @IsNotEmpty()
  @MaxLength(200)
  idempotencyKey?: string;

  @IsOptional()
  @IsString()
  subject?: string;

  @IsString()
  @IsNotEmpty()
  @MinLength(1)
  message!: string;

  @IsOptional()
  @IsEnum(ContentTypeDto)
  contentType: ContentTypeDto = ContentTypeDto.text;

  @IsArray()
  @ArrayMinSize(1)
  @IsEnum(NotificationChannelDto, { each: true })
  channels!: NotificationChannelDto[];

  @IsString()
  @IsNotEmpty()
  source!: string;
}
