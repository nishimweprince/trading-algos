import { createHash } from 'crypto';
import { AppConfigService } from '../config/app-config.service';
import { TradingIdea } from '../models/trading-idea.model';
import {
  Mt5ExecutionRecord,
  Mt5SignalRequest,
} from './mt5.types';

// RFC 4122 URL namespace. The name prefix permanently scopes IDs to this integration.
const UUID_URL_NAMESPACE = '6ba7b811-9dad-11d1-80b4-00c04fd430c8';

export function deterministicSignalId(ideaHash: string): string {
  const digest = createHash('sha1')
    .update(UUID_URL_NAMESPACE.replace(/-/g, ''), 'hex')
    .update(`signals-scrapper:mt5:${ideaHash}`, 'utf8')
    .digest();
  const bytes = Buffer.from(digest.subarray(0, 16));
  bytes[6] = (bytes[6] & 0x0f) | 0x50;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = bytes.toString('hex');
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

export class Mt5SignalMapper {
  constructor(private readonly config: AppConfigService) {}

  createRecords(ideas: TradingIdea[]): Mt5ExecutionRecord[] {
    if (!this.config.mt5SignalTradingEnabled) return [];
    return ideas
      .filter((idea) => idea.provider === 'TRADING_CENTRAL')
      .filter((idea) => idea.direction === 'UP' || idea.direction === 'DOWN')
      .map((idea) => this.createRecord(idea));
  }

  private createRecord(idea: TradingIdea): Mt5ExecutionRecord {
    const now = new Date().toISOString();
    const signalId = deterministicSignalId(idea.hash);
    const rule = this.config.mt5SignalRules[idea.instrument];
    if (!rule) {
      return this.skipped(signalId, idea, now, 'unmapped_instrument');
    }

    const stopLoss = idea.stopLoss ?? idea.pivot;
    const takeProfit = idea.takeProfit ?? idea.target;
    if (
      stopLoss === undefined ||
      takeProfit === undefined ||
      !Number.isFinite(stopLoss) ||
      !Number.isFinite(takeProfit)
    ) {
      return this.skipped(signalId, idea, now, 'missing_risk_levels');
    }

    const request: Mt5SignalRequest = {
      signal_id: signalId,
      occurred_at: idea.capturedAt,
      execution_type: 'market',
      symbol: rule.symbol,
      direction: idea.direction === 'UP' ? 'buy' : 'sell',
      volume: rule.volume,
      stop_loss: String(stopLoss),
      take_profit: String(takeProfit),
      note: `Trading Central ${idea.instrument} ${idea.timeframe} [${idea.hash.slice(0, 12)}]`,
    };
    return {
      signalId,
      ideaHash: idea.hash,
      status: 'pending',
      request,
      attempts: 0,
      createdAt: now,
      updatedAt: now,
    };
  }

  private skipped(
    signalId: string,
    idea: TradingIdea,
    now: string,
    code: string,
  ): Mt5ExecutionRecord {
    return {
      signalId,
      ideaHash: idea.hash,
      status: 'skipped',
      attempts: 0,
      createdAt: now,
      updatedAt: now,
      error: {
        code,
        message:
          code === 'unmapped_instrument'
            ? `No MT5_SIGNAL_RULES entry exists for ${idea.instrument}`
            : 'The signal does not contain both stop-loss and take-profit levels',
      },
    };
  }
}
