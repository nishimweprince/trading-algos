import { Bot } from 'grammy';
import type { Config } from '../config/schema.ts';
import { readSecret } from '../config/load.ts';
import { registerSecret, logger } from '../core/logger.ts';
import type { TypedBus } from '../core/bus.ts';

/**
 * Telegram alerting (Section 2/8/12). Telegram delivery is intentionally
 * limited to alerts that opt in, while all alerts are logged and can be
 * recorded for the dashboard. When no bot token is configured the alerter
 * degrades to a logging-only no-op so the bot still runs in paper mode without
 * Telegram set up.
 *
 * Admin commands (/kill, blacklist edits) are gated to config.alerts.adminUserIds
 * and wired in later phases; the token/chat plumbing lives here.
 */

const LEVEL_EMOJI = { info: 'ℹ️', warn: '⚠️', error: '🛑' } as const;

export class Alerter {
  private readonly bot: Bot | null;
  private readonly chatId: string | number | undefined;
  private readonly log = logger.child({ mod: 'telegram' });
  private polling = false;

  private constructor(bot: Bot | null, chatId: string | number | undefined) {
    this.bot = bot;
    this.chatId = chatId;
  }

  static create(config: Config): Alerter {
    const token = readSecret(config.alerts.telegramBotTokenEnvVar);
    const chatId = config.alerts.chatId;

    if (!token) {
      logger.child({ mod: 'telegram' }).warn(
        'no Telegram bot token configured — alerts will log only',
        { envVar: config.alerts.telegramBotTokenEnvVar },
      );
      return new Alerter(null, chatId);
    }
    registerSecret(token);
    if (!chatId) {
      logger.child({ mod: 'telegram' }).warn('Telegram token set but alerts.chatId missing — cannot deliver');
    }
    return new Alerter(new Bot(token), chatId);
  }

  private async send(text: string): Promise<void> {
    if (!this.bot || !this.chatId) return;
    try {
      await this.bot.api.sendMessage(this.chatId, text, { parse_mode: 'HTML' });
    } catch (err) {
      // Never let a failed alert crash the trading loop.
      this.log.error('failed to send Telegram message', { err });
    }
  }

  /** Wire push-enabled bus `alert` events to Telegram. Returns an unsubscribe fn. */
  attach(bus: TypedBus): () => void {
    return bus.on('alert', (a) => {
      if (a.telegram === true) void this.send(`${LEVEL_EMOJI[a.level]} ${a.message}`);
      const line = this.log[a.level === 'warn' ? 'warn' : a.level === 'error' ? 'error' : 'info'];
      line('alert', { message: a.message, telegram: a.telegram === true });
    });
  }

  startupMessage(config: Config, bus: TypedBus): void {
    bus.emit('alert', {
      level: 'info',
      message:
        `pump.fun scalper online — mode: ${config.mode}; ` +
        `base size: ${config.entry.baseSizeSol} SOL; max concurrent: ${config.risk.maxConcurrentPositions}`,
    });
    this.log.info('startup notification recorded', { mode: config.mode });
  }

  /**
   * Start admin command handling (long polling): `/kill` engages the global kill
   * switch, `/status` reports risk + position state. Both are gated to
   * `adminUserIds`; a message from anyone else is ignored. No-op without a bot
   * token or admin list. Command ingress can never block the trading loop.
   */
  startCommands(deps: { bus: TypedBus; adminUserIds: number[]; getStatus: () => string }): void {
    if (!this.bot || deps.adminUserIds.length === 0) {
      this.log.info('telegram commands not started', {
        hasBot: Boolean(this.bot),
        admins: deps.adminUserIds.length,
      });
      return;
    }
    const admins = new Set(deps.adminUserIds);
    const gate = (fromId: number | undefined): boolean => {
      if (fromId !== undefined && admins.has(fromId)) return true;
      this.log.warn('rejected admin command from non-admin', { fromId });
      return false;
    };

    this.bot.command('kill', async (ctx) => {
      if (!gate(ctx.from?.id)) return;
      deps.bus.emit('killSwitch', { source: 'telegram', detail: `user ${ctx.from?.id}` });
      await ctx.reply('🛑 kill switch engaged — flattening positions, entries halted.');
    });
    this.bot.command('status', async (ctx) => {
      if (!gate(ctx.from?.id)) return;
      await ctx.reply(deps.getStatus());
    });

    // Long polling — fire and forget; failures are logged, never thrown. The
    // "Aborted delay" that grammy surfaces when bot.stop() cancels an in-flight
    // getUpdates is expected shutdown behavior, not an error.
    void this.bot.start({ drop_pending_updates: true }).catch((err) => {
      if (/abort/i.test((err as Error)?.message ?? '')) this.log.debug('telegram polling aborted (shutdown)');
      else this.log.error('telegram polling failed', { err });
    });
    this.polling = true;
    this.log.info('telegram admin commands started', { admins: deps.adminUserIds.length });
  }

  async stop(): Promise<void> {
    if (this.bot && this.polling) {
      try {
        await this.bot.stop();
      } catch (err) {
        this.log.error('telegram bot stop failed', { err });
      }
      this.polling = false;
    }
  }
}
