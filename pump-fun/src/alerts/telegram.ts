import { Bot } from 'grammy';
import type { Config } from '../config/schema.ts';
import { readSecret } from '../config/load.ts';
import { registerSecret, logger } from '../core/logger.ts';
import type { TypedBus } from '../core/bus.ts';

/**
 * Telegram alerting (Section 2/8/12). Surfaces trades, vetoes, and
 * circuit-breaker events to the operator. When no bot token is configured the
 * alerter degrades to a logging-only no-op so the bot still runs in paper mode
 * without Telegram set up.
 *
 * Admin commands (/kill, blacklist edits) are gated to config.alerts.adminUserIds
 * and wired in later phases; the token/chat plumbing lives here.
 */

const LEVEL_EMOJI = { info: 'ℹ️', warn: '⚠️', error: '🛑' } as const;

export class Alerter {
  private readonly bot: Bot | null;
  private readonly chatId: string | number | undefined;
  private readonly log = logger.child({ mod: 'telegram' });

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

  /** Wire bus `alert` events to Telegram. Returns an unsubscribe fn. */
  attach(bus: TypedBus): () => void {
    return bus.on('alert', (a) => {
      void this.send(`${LEVEL_EMOJI[a.level]} ${a.message}`);
      const line = this.log[a.level === 'warn' ? 'warn' : a.level === 'error' ? 'error' : 'info'];
      line('alert', { message: a.message });
    });
  }

  async startupMessage(config: Config): Promise<void> {
    const msg =
      `🚀 <b>pump.fun scalper online</b>\n` +
      `mode: <b>${config.mode}</b>\n` +
      `base size: ${config.entry.baseSizeSol} SOL · max concurrent: ${config.risk.maxConcurrentPositions}\n` +
      `<i>Not financial advice. Paper mode default.</i>`;
    await this.send(msg);
    this.log.info('startup alert dispatched', { mode: config.mode, delivered: Boolean(this.bot && this.chatId) });
  }

  async stop(): Promise<void> {
    // Bot is not long-polling in Phase 0; nothing to tear down yet.
  }
}
