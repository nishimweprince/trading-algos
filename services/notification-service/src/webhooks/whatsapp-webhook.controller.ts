import {
  Body,
  Controller,
  ForbiddenException,
  Get,
  Headers,
  HttpCode,
  Logger,
  Post,
  Query,
  Req,
  Res,
  UnauthorizedException,
} from '@nestjs/common';
import { Request, Response } from 'express';
import { WhatsappChannel } from '../channels/whatsapp.channel';
import { DispatcherService } from '../notifications/dispatcher.service';

@Controller('webhooks')
export class WhatsappWebhookController {
  private readonly logger = new Logger(WhatsappWebhookController.name);

  constructor(
    private readonly whatsapp: WhatsappChannel,
    private readonly dispatcher: DispatcherService,
  ) {}

  @Get('whatsapp')
  verify(
    @Query('hub.mode') mode: string | undefined,
    @Query('hub.verify_token') token: string | undefined,
    @Query('hub.challenge') challenge: string | undefined,
    @Res() res: Response,
  ): void {
    const result = this.whatsapp.verifyWebhookChallenge(mode, token, challenge);
    if (result === null) {
      throw new ForbiddenException('Verification failed');
    }
    res.status(200).type('text/plain').send(result);
  }

  @Post('whatsapp')
  @HttpCode(200)
  async receive(
    @Req() req: Request,
    @Headers('x-hub-signature-256') signature: string | undefined,
  ): Promise<{ ok: boolean }> {
    const rawBody = (req as Request & { rawBody?: Buffer }).rawBody;
    if (!rawBody || !this.whatsapp.verifySignature(rawBody, signature)) {
      this.logger.warn('Rejecting webhook: signature mismatch');
      throw new UnauthorizedException('Invalid signature');
    }

    let payload: unknown;
    try {
      payload = JSON.parse(rawBody.toString('utf8'));
    } catch {
      this.logger.warn('Webhook body was not JSON; ignoring');
      return { ok: true };
    }

    await this.processStatusEvents(payload);
    return { ok: true };
  }

  private async processStatusEvents(payload: unknown): Promise<void> {
    if (!payload || typeof payload !== 'object') return;

    const entries = (payload as { entry?: unknown[] }).entry ?? [];
    for (const entry of entries) {
      if (!entry || typeof entry !== 'object') continue;
      const changes = (entry as { changes?: unknown[] }).changes ?? [];
      for (const change of changes) {
        if (!change || typeof change !== 'object') continue;
        const value = (change as { value?: Record<string, unknown> }).value ?? {};
        const statuses = (value.statuses as unknown[]) ?? [];
        for (const statusEvent of statuses) {
          if (!statusEvent || typeof statusEvent !== 'object') continue;
          const event = statusEvent as {
            id?: string;
            status?: string;
            errors?: Array<Record<string, unknown>>;
          };
          const wamid = event.id;
          const newStatus = event.status;
          let error: string | undefined;
          if (Array.isArray(event.errors)) {
            error = event.errors
              .map(
                (e) =>
                  `${e.code}:${e.title}:${String(e.message ?? '')}`,
              )
              .join('; ');
          }
          if (wamid && newStatus) {
            const updated = await this.dispatcher.updateStatusByExternalId(
              wamid,
              newStatus,
              error,
            );
            this.logger.log(
              `Webhook status: wamid=${wamid} status=${newStatus} updated=${updated}`,
            );
          }
        }
      }
    }
  }
}
