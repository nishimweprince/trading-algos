import { FloodWaitError } from 'telegram/errors';
import type { TelegramClient } from 'telegram';
import type { AppConfig } from '../config.js';
import type { AppLogger } from '../logger.js';
import type { JsonlWriter } from '../state/jsonlLog.js';
import type { CursorsFile } from '../state/cursorStore.js';
import { writeCursorsAtomic } from '../state/cursorStore.js';
import { withOneRetry, sleepMs } from '../util/retry.js';
import { toIso } from '../util/time.js';
import { resolveChannel, type ResolvedChannel } from '../telegram/channelResolver.js';
import {
  getLatestMessage,
  getMessageText,
  getMessagesSince,
  messageDateIso,
} from '../telegram/poller.js';
import { parseMessageText, parsedToSignal } from '../signals/parser.js';
import type { Deduper } from '../signals/deduper.js';
import { notifySignal, type NotifyDeps } from '../notifications/dispatcher.js';

export type TickCounters = {
  messagesPolledTotal: number;
  signalsDetectedTotal: number;
  smsAttemptsTotal: number;
  smsFailuresTotal: number;
};

export type ChannelTickResult = {
  skipRestOfTick: boolean;
  floodWaitSeconds?: number;
};

export async function processChannelTick(
  client: TelegramClient,
  config: AppConfig,
  logger: AppLogger,
  resolved: ResolvedChannel,
  cursors: CursorsFile,
  deduper: Deduper,
  signalsLog: JsonlWriter,
  errorsLog: JsonlWriter,
  rawLog: JsonlWriter,
  notifyDeps: NotifyDeps,
  counters: TickCounters,
): Promise<ChannelTickResult> {
  const { username, entity, channelId } = resolved;
  const nowIso = toIso();

  let row = cursors[username];
  if (!row) {
    row = { channelId, lastSeenMessageId: 0, updatedAt: nowIso };
    cursors[username] = row;
  } else {
    row.channelId = channelId;
  }

  const primeIfNeeded = async (): Promise<boolean> => {
    if (row!.lastSeenMessageId > 0) return false;
    const latest = await withOneRetry(
      () => getLatestMessage(client, entity),
      2000,
      (e) => logger.warn({ err: e, channel: username }, 'getLatestMessage retry'),
    );
    const id = latest ? Number(latest.id) : 0;
    row!.lastSeenMessageId = id;
    row!.updatedAt = toIso();
    await writeCursorsAtomic(config.resolvedCursorsPath, cursors);
    logger.info({ channel: username, lastSeenMessageId: id }, 'primed cursor (skip ahead to now)');
    return true;
  };

  try {
    const primed = await primeIfNeeded();
    if (primed) return { skipRestOfTick: false };

    const minId = row!.lastSeenMessageId;
    const messages = await withOneRetry(
      () => getMessagesSince(client, entity, minId, config.POLL_MAX_MESSAGES_PER_CHANNEL),
      2000,
      (e) => logger.warn({ err: e, channel: username }, 'getMessagesSince retry'),
    );

    counters.messagesPolledTotal += messages.length;

    for (const m of messages) {
      const mid = Number(m.id);
      if (deduper.alreadyHandled(channelId, mid)) {
        row!.lastSeenMessageId = Math.max(row!.lastSeenMessageId, mid);
        continue;
      }

      const text = getMessageText(m);
      const msgDate = messageDateIso(m);

      if (config.LOG_RAW_MESSAGES) {
        rawLog.append({
          ts: toIso(),
          sourceChannel: username,
          messageId: mid,
          messageDate: msgDate,
          text,
        });
      }

      const parsed = parseMessageText(text, {
        sourceChannel: username,
        messageId: mid,
        messageDate: msgDate,
      });

      if (!parsed.isSignal) {
        if (parsed.reason === 'ambiguous_direction') {
          errorsLog.append({
            ts: toIso(),
            kind: 'parser_ambiguous',
            details: { channel: username, messageId: mid, reason: parsed.reason, text },
          });
        }
        row!.lastSeenMessageId = Math.max(row!.lastSeenMessageId, mid);
        continue;
      }

      const signal = parsedToSignal(parsed, channelId);
      counters.signalsDetectedTotal += 1;
      signalsLog.append({
        ts: toIso(),
        signalId: signal.id,
        sourceChannel: signal.sourceChannel,
        messageId: signal.messageId,
        messageDate: signal.messageDate,
        symbol: signal.symbol,
        direction: signal.direction,
        rawText: signal.rawText,
      });
      await signalsLog.flush();

      await notifySignal(signal, notifyDeps);

      row!.lastSeenMessageId = Math.max(row!.lastSeenMessageId, mid);
      row!.updatedAt = toIso();
      await writeCursorsAtomic(config.resolvedCursorsPath, cursors);
    }

    row!.updatedAt = toIso();
    await writeCursorsAtomic(config.resolvedCursorsPath, cursors);
    await rawLog.flush();
    await errorsLog.flush();

    return { skipRestOfTick: false };
  } catch (e) {
    if (e instanceof FloodWaitError) {
      const sec = e.seconds;
      logger.warn({ channel: username, seconds: sec }, 'telegram flood wait');
      errorsLog.append({
        ts: toIso(),
        kind: 'telegram_flood_wait',
        details: { channel: username, seconds: sec },
      });
      await errorsLog.flush();
      await sleepMs((sec + 1) * 1000);
      return { skipRestOfTick: true, floodWaitSeconds: sec };
    }
    logger.error({ err: e, channel: username }, 'channel tick failed');
    errorsLog.append({
      ts: toIso(),
      kind: 'telegram_error',
      details: { channel: username, error: e instanceof Error ? e.message : String(e) },
    });
    await errorsLog.flush();
    return { skipRestOfTick: false };
  }
}

export async function resolveAllChannels(
  client: TelegramClient,
  config: AppConfig,
  logger: AppLogger,
  errorsLog: JsonlWriter,
): Promise<ResolvedChannel[]> {
  const out: ResolvedChannel[] = [];
  for (const ch of config.TELEGRAM_CHANNELS) {
    try {
      out.push(await resolveChannel(client, ch));
    } catch (e) {
      logger.error({ err: e, channel: ch }, 'failed to resolve channel');
      errorsLog.append({
        ts: toIso(),
        kind: 'channel_resolve_failed',
        details: { channel: ch, error: e instanceof Error ? e.message : String(e) },
      });
      await errorsLog.flush();
    }
  }
  return out;
}
