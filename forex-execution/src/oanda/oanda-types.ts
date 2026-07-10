export interface OandaAccountSummaryResponse {
  account: OandaAccount;
  lastTransactionID: string;
}

export interface OandaAccountDetailsResponse {
  account: OandaAccount;
  lastTransactionID: string;
}

export interface OandaAccountInstrumentsResponse {
  instruments: OandaInstrument[];
  lastTransactionID: string;
}

export interface OandaAccountChangesResponse {
  changes: Record<string, unknown>;
  state: Record<string, unknown>;
  lastTransactionID: string;
}

export interface OandaAccount {
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
}

export interface OandaInstrument {
  name: string;
  type: string;
  displayName: string;
  pipLocation: number;
  displayPrecision: number;
  tradeUnitsPrecision: number;
  minimumTradeSize: string;
  maximumOrderUnits?: string;
  marginRate: string;
  minimumTrailingStopDistance?: string;
  financing?: Record<string, unknown>;
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

export type AccountDetails = AccountSummary;

export interface TradableInstrument {
  name: string;
  type: string;
  displayName: string;
  pipLocation: number;
  displayPrecision: number;
  tradeUnitsPrecision: number;
  minimumTradeSize: string;
  maximumOrderUnits?: string;
  marginRate: string;
  minimumTrailingStopDistance?: string;
  financing?: Record<string, unknown>;
}

export interface AccountChanges {
  changes: Record<string, unknown>;
  state: Record<string, unknown>;
  lastTransactionId: string;
}
