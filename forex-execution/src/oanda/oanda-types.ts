export interface OandaAccountSummaryResponse {
  account: {
    id: string;
    currency: string;
    balance: string;
    NAV: string;
    marginAvailable: string;
    marginUsed: string;
    unrealizedPL: string;
    pl: string;
    openTradeCount: number;
    openPositionCount: number;
    pendingOrderCount: number;
    lastTransactionID: string;
    hedgingEnabled: boolean;
  };
  lastTransactionID: string;
}

export interface AccountSummary {
  accountId: string;
  currency: string;
  balance: string;
  nav: string;
  marginAvailable: string;
  marginUsed: string;
  unrealizedPL: string;
  realizedPL: string;
  openTradeCount: number;
  openPositionCount: number;
  pendingOrderCount: number;
  lastTransactionId: string;
  hedgingEnabled: boolean;
}
