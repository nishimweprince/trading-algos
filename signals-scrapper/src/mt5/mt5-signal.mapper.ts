import { createHash } from 'crypto';
import { AppConfigService } from '../config/app-config.service';
import { TradingIdea } from '../models/trading-idea.model';
import {
  Mt5ExecutionRecord,
  Mt5SignalRequest,
} from './mt5.types';

// RFC 4122 URL namespace. The name prefix permanently scopes IDs to this integration.
const UUID_URL_NAMESPACE = '6ba7b811-9dad-11d1-80b4-00c04fd430c8';

// OCR/LLM decimal-placement errors (e.g. "0.8522" instead of "148.522" for a
// JPY pair) produce exit levels off from entry by orders of magnitude. Real
// stop-loss/take-profit distances stay within a small multiple of entry, so
// this band only trips on genuine scale errors, not wide-but-legitimate stops.
const MIN_EXIT_LEVEL_TO_ENTRY_RATIO = 0.2;
const MAX_EXIT_LEVEL_TO_ENTRY_RATIO = 5;

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
      .filter(
        (idea) =>
          idea.provider === 'TRADING_CENTRAL' ||
          idea.provider === 'AUTOCHARTIST',
      )
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

    const hasValidRiskRelationship =
      idea.direction === 'UP'
        ? stopLoss < takeProfit
        : stopLoss > takeProfit;
    if (!hasValidRiskRelationship) {
      return this.skipped(signalId, idea, now, 'invalid_risk_relationship');
    }

    if (this.hasImplausibleExitLevelMagnitude(idea.entry, stopLoss, takeProfit)) {
      return this.skipped(signalId, idea, now, 'implausible_exit_level_magnitude');
    }

    const request: Mt5SignalRequest = {
      signal_id: signalId,
      // Autochartist cards carry an "identified at" age and its slow capture
      // flow (SSO + Our Favourites click + vision) can push capturedAt past the
      // MT5 freshness window, so date the order at queue time instead. Trading
      // Central keeps its capture timestamp.
      occurred_at: idea.provider === 'AUTOCHARTIST' ? now : idea.capturedAt,
      execution_type: 'market',
      symbol: rule.symbol,
      direction: idea.direction === 'UP' ? 'buy' : 'sell',
      volume: rule.volume,
      stop_loss: String(stopLoss),
      take_profit: String(takeProfit),
      note: idea.hash,
      source:
        idea.provider === 'AUTOCHARTIST' ? 'autochartist' : 'trading_central',
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

  private hasImplausibleExitLevelMagnitude(
    entry: number | null | undefined,
    stopLoss: number,
    takeProfit: number,
  ): boolean {
    if (entry === null || entry === undefined || !Number.isFinite(entry) || entry <= 0) {
      return false;
    }
    const isImplausible = (level: number) => {
      const ratio = level / entry;
      return ratio < MIN_EXIT_LEVEL_TO_ENTRY_RATIO || ratio > MAX_EXIT_LEVEL_TO_ENTRY_RATIO;
    };
    return isImplausible(stopLoss) || isImplausible(takeProfit);
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
        message: this.skipMessage(code, idea),
      },
    };
  }

  private skipMessage(code: string, idea: TradingIdea): string {
    if (code === 'unmapped_instrument') {
      return `No MT5_SIGNAL_RULES entry exists for ${idea.instrument}`;
    }
    if (code === 'invalid_risk_relationship') {
      return idea.direction === 'UP'
        ? 'Buy signal requires stop-loss below take-profit'
        : 'Sell signal requires stop-loss above take-profit';
    }
    if (code === 'implausible_exit_level_magnitude') {
      return `Stop-loss/take-profit magnitude is implausible relative to entry price (${idea.entry})`;
    }
    return 'The signal does not contain both stop-loss and take-profit levels';
  }
}
