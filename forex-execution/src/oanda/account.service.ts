import { AppConfig } from '../config/env.js';
import { AccountRepository } from '../persistence/repositories/account.repository.js';
import { OandaClient } from './oanda-client.js';
import {
  AccountChanges,
  AccountDetails,
  AccountSummary,
  OandaAccount,
  OandaAccountChangesResponse,
  OandaAccountDetailsResponse,
  OandaAccountInstrumentsResponse,
  OandaAccountSummaryResponse,
  OandaInstrument,
  TradableInstrument,
} from './oanda-types.js';

export class AccountService {
  constructor(
    private readonly client: OandaClient,
    private readonly config: AppConfig,
    private readonly repository?: AccountRepository,
  ) {}

  async getSummary(): Promise<AccountSummary> {
    const response = await this.client.get<OandaAccountSummaryResponse>(`/v3/accounts/${this.config.OANDA_ACCOUNT_ID}/summary`);
    const summary = normalizeAccount(response.account);
    await this.repository?.createSnapshot(summary, response as unknown as Record<string, unknown>);
    return summary;
  }

  async getDetails(): Promise<AccountDetails> {
    const response = await this.client.get<OandaAccountDetailsResponse>(`/v3/accounts/${this.config.OANDA_ACCOUNT_ID}`);
    const details = normalizeAccount(response.account);
    await this.repository?.createSnapshot(details, response as unknown as Record<string, unknown>);
    return details;
  }

  async listInstruments(): Promise<TradableInstrument[]> {
    const account = await this.getSummary();
    const response = await this.client.get<OandaAccountInstrumentsResponse>(`/v3/accounts/${this.config.OANDA_ACCOUNT_ID}/instruments`);
    const instruments = response.instruments.map(normalizeInstrument);
    const rawByName = new Map(response.instruments.map((instrument) => [instrument.name, instrument as unknown as Record<string, unknown>]));
    await this.repository?.upsertInstruments(account, instruments, rawByName);
    return instruments;
  }

  async getChangesSince(transactionId: string): Promise<AccountChanges> {
    const response = await this.client.get<OandaAccountChangesResponse>(`/v3/accounts/${this.config.OANDA_ACCOUNT_ID}/changes?sinceTransactionID=${encodeURIComponent(transactionId)}`);
    return { changes: response.changes, state: response.state, lastTransactionId: response.lastTransactionID };
  }
}

function normalizeAccount(account: OandaAccount): AccountSummary {
  return {
    accountId: account.id,
    currency: account.currency,
    balance: account.balance,
    nav: account.NAV,
    marginAvailable: account.marginAvailable,
    marginUsed: account.marginUsed,
    unrealizedPL: account.unrealizedPL,
    realizedPL: account.pl,
    openTradeCount: account.openTradeCount,
    openPositionCount: account.openPositionCount,
    pendingOrderCount: account.pendingOrderCount,
    lastTransactionId: account.lastTransactionID,
    hedgingEnabled: account.hedgingEnabled,
  };
}

function normalizeInstrument(instrument: OandaInstrument): TradableInstrument {
  return {
    name: instrument.name,
    type: instrument.type,
    displayName: instrument.displayName,
    pipLocation: instrument.pipLocation,
    displayPrecision: instrument.displayPrecision,
    tradeUnitsPrecision: instrument.tradeUnitsPrecision,
    minimumTradeSize: instrument.minimumTradeSize,
    ...(instrument.maximumOrderUnits === undefined ? {} : { maximumOrderUnits: instrument.maximumOrderUnits }),
    marginRate: instrument.marginRate,
    ...(instrument.minimumTrailingStopDistance === undefined ? {} : { minimumTrailingStopDistance: instrument.minimumTrailingStopDistance }),
    ...(instrument.financing === undefined ? {} : { financing: instrument.financing }),
  };
}
