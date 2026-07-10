import { AppConfig } from '../config/env.js';
import { OandaClient } from './oanda-client.js';
import { AccountSummary, OandaAccountSummaryResponse } from './oanda-types.js';

export class AccountService {
  constructor(private readonly client: OandaClient, private readonly config: AppConfig) {}

  async getSummary(): Promise<AccountSummary> {
    const response = await this.client.get<OandaAccountSummaryResponse>(`/v3/accounts/${this.config.OANDA_ACCOUNT_ID}/summary`);
    const account = response.account;
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
}
