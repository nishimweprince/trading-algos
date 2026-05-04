import type { AppConfig } from '../config.js';
import type { AppLogger } from '../logger.js';
import type { JsonlWriter } from '../state/jsonlLog.js';
import { toIso } from '../util/time.js';
import { formatSignal } from './formatter.js';
import type { PindoClient } from './pindoClient.js';
import type { Signal } from '../signals/types.js';
import { Deduper } from '../signals/deduper.js';
import type { Deduper as DeduperType } from '../signals/deduper.js';

export type NotifyDeps = {
  config: AppConfig;
  logger: AppLogger;
  pindo: PindoClient;
  smsLog: JsonlWriter;
  errorsLog: JsonlWriter;
  deduper: DeduperType;
  handledPath: string;
  onSmsAttempt?: () => void;
  onSmsFailure?: () => void;
};

export async function notifySignal(signal: Signal, deps: NotifyDeps): Promise<void> {
  const { config, logger, pindo, smsLog, errorsLog, deduper, handledPath, onSmsAttempt, onSmsFailure } =
    deps;

  if (!config.NOTIFICATIONS_ENABLED || config.NOTIFICATION_NUMBERS.length === 0) {
    logger.warn(
      { signalId: signal.id, notificationsEnabled: config.NOTIFICATIONS_ENABLED },
      'notifications disabled or no recipients',
    );
    return;
  }

  const body = formatSignal(signal, config.PINDO_SENDER_ID);

  for (const recipient of config.NOTIFICATION_NUMBERS) {
    onSmsAttempt?.();
    let result;
    try {
      result = await pindo.sendSms(recipient, body);
    } catch (e) {
      onSmsFailure?.();
      const errMsg = e instanceof Error ? e.message : String(e);
      smsLog.append({
        ts: toIso(),
        signalId: signal.id,
        recipient,
        body,
        ok: false,
        status: 0,
        error: `transport: ${errMsg}`,
        channel: signal.sourceChannel,
      });
      errorsLog.append({
        ts: toIso(),
        kind: 'sms_failed',
        details: { recipient, signalId: signal.id, error: errMsg },
      });
      continue;
    }

    smsLog.append({
      ts: toIso(),
      signalId: signal.id,
      recipient,
      body,
      ok: result.ok,
      status: result.ok ? 200 : result.status,
      error: result.ok ? null : JSON.stringify(result.body),
      channel: signal.sourceChannel,
    });

    if (!result.ok) {
      onSmsFailure?.();
      errorsLog.append({
        ts: toIso(),
        kind: 'sms_failed',
        details: {
          recipient,
          signalId: signal.id,
          status: result.status,
          body: result.body,
        },
      });
    }
  }

  deduper.markHandledMemory(signal.channelId, signal.messageId);
  await Deduper.persistAtomic(handledPath, deduper.toFile());
  await smsLog.flush();
  await errorsLog.flush();
}
