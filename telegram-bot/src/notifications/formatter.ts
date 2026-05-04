import type { Signal } from '../signals/types.js';
import { formatSmsUtc } from '../util/time.js';

const SMS_MAX = 150;

export function formatSignal(signal: Signal, company: string): string {
  const when = formatSmsUtc(new Date(signal.messageDate));
  const dir = signal.direction;
  const sym = signal.symbol;
  const ch = signal.sourceChannel;
  const base = `[${company}] ${sym} ${dir}  ${when}  via ${ch}`;
  if (base.length <= SMS_MAX) return base;
  const prefix = `[${company}] ${sym} ${dir}  ${when}  via `;
  const budget = SMS_MAX - prefix.length;
  if (budget < 8) {
    return base.slice(0, SMS_MAX);
  }
  const truncated = ch.length > budget ? `${ch.slice(0, budget - 3)}...` : ch;
  return `${prefix}${truncated}`;
}
