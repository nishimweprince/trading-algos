import { AccountService } from './account.service.js';
import { TradableInstrument } from './oanda-types.js';

const instrumentCacheTtlMs = 24 * 60 * 60 * 1000;

export class InstrumentService {
  private cachedInstruments = new Map<string, TradableInstrument>();
  private refreshedAt = 0;

  constructor(private readonly accountService: AccountService) {}

  async refresh(): Promise<TradableInstrument[]> {
    const instruments = await this.accountService.listInstruments();
    this.cachedInstruments = new Map(instruments.map((instrument) => [instrument.name, instrument]));
    this.refreshedAt = Date.now();
    return instruments;
  }

  async list(): Promise<TradableInstrument[]> {
    if (this.isCacheFresh()) return [...this.cachedInstruments.values()];
    return this.refresh();
  }

  async get(instrumentName: string): Promise<TradableInstrument | undefined> {
    if (!this.isCacheFresh()) await this.refresh();
    return this.cachedInstruments.get(instrumentName);
  }

  isCacheFresh(now = Date.now()): boolean {
    return this.cachedInstruments.size > 0 && now - this.refreshedAt < instrumentCacheTtlMs;
  }
}
