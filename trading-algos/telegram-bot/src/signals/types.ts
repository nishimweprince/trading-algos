export type Direction = 'BUY' | 'SELL';

export type CanonicalSymbol = 'XAU';

export interface Signal {
  id: string;
  symbol: CanonicalSymbol;
  direction: Direction;
  sourceChannel: string;
  messageId: number;
  messageDate: string;
  rawText: string;
  channelId: string;
}

export type ParsedMessage =
  | { isSignal: false; reason: string }
  | {
      isSignal: true;
      symbol: CanonicalSymbol;
      direction: Direction;
      sourceChannel: string;
      messageId: number;
      messageDate: string;
      rawText: string;
    };

export type ChannelParseConfig = Record<string, unknown>;
