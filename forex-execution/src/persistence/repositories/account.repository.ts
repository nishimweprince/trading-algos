import { Prisma, PrismaClient } from '@prisma/client';
import { AppConfig } from '../../config/env.js';
import { AccountSummary, TradableInstrument } from '../../oanda/oanda-types.js';

export class AccountRepository {
  constructor(private readonly prismaClient: PrismaClient, private readonly config: AppConfig) {}

  async upsertBrokerAccount(summary: AccountSummary): Promise<{ id: string }> {
    return this.prismaClient.brokerAccount.upsert({
      where: { broker_accountId_environment: { broker: 'OANDA', accountId: summary.accountId, environment: this.config.OANDA_ENV } },
      create: {
        broker: 'OANDA',
        accountId: summary.accountId,
        alias: this.config.OANDA_ACCOUNT_ALIAS,
        environment: this.config.OANDA_ENV,
        currency: summary.currency,
      },
      update: { alias: this.config.OANDA_ACCOUNT_ALIAS, currency: summary.currency },
      select: { id: true },
    });
  }

  async createSnapshot(summary: AccountSummary, rawData: Record<string, unknown>): Promise<void> {
    const brokerAccount = await this.upsertBrokerAccount(summary);
    await this.prismaClient.accountSnapshot.create({
      data: {
        brokerAccountId: brokerAccount.id,
        balance: summary.balance,
        nav: summary.nav,
        marginUsed: summary.marginUsed,
        marginAvailable: summary.marginAvailable,
        unrealizedPL: summary.unrealizedPL,
        realizedPL: summary.realizedPL,
        openTradeCount: summary.openTradeCount,
        openPositionCount: summary.openPositionCount,
        pendingOrderCount: summary.pendingOrderCount,
        lastTransactionId: summary.lastTransactionId,
        rawData: toInputJson(rawData),
      },
    });
  }

  async upsertInstruments(account: AccountSummary, instruments: TradableInstrument[], rawByName: Map<string, Record<string, unknown>>): Promise<void> {
    const brokerAccount = await this.upsertBrokerAccount(account);
    await this.prismaClient.$transaction(
      instruments.map((instrument) => {
        const metadata = buildInstrumentMetadata(instrument, rawByName.get(instrument.name) ?? {});
        return this.prismaClient.instrumentMetadata.upsert({
          where: { brokerAccountId_name: { brokerAccountId: brokerAccount.id, name: instrument.name } },
          create: { brokerAccountId: brokerAccount.id, ...metadata },
          update: metadata,
        });
      }),
    );
  }
  async listSnapshots(limit: number): Promise<unknown[]> {
    const account = await this.prismaClient.brokerAccount.findUnique({
      where: { broker_accountId_environment: { broker: 'OANDA', accountId: this.config.OANDA_ACCOUNT_ID, environment: this.config.OANDA_ENV } },
      select: { id: true },
    });
    if (!account) return [];
    return this.prismaClient.accountSnapshot.findMany({
      where: { brokerAccountId: account.id },
      orderBy: { capturedAt: 'desc' },
      take: limit,
    });
  }
}

function buildInstrumentMetadata(
  instrument: TradableInstrument,
  rawData: Record<string, unknown>,
): Omit<Prisma.InstrumentMetadataCreateInput, 'brokerAccount'> {
  return {
    name: instrument.name,
    displayName: instrument.displayName,
    type: instrument.type,
    pipLocation: instrument.pipLocation,
    displayPrecision: instrument.displayPrecision,
    tradeUnitsPrecision: instrument.tradeUnitsPrecision,
    minimumTradeSize: instrument.minimumTradeSize,
    ...(instrument.maximumOrderUnits === undefined ? {} : { maximumOrderUnits: instrument.maximumOrderUnits }),
    marginRate: instrument.marginRate,
    ...(instrument.minimumTrailingStopDistance === undefined ? {} : { minimumTrailingStopDistance: instrument.minimumTrailingStopDistance }),
    ...(instrument.financing === undefined ? {} : { financing: toInputJson(instrument.financing) }),
    rawData: toInputJson(rawData),
    refreshedAt: new Date(),
  };
}

function toInputJson(value: unknown): Prisma.InputJsonValue {
  return JSON.parse(JSON.stringify(value)) as Prisma.InputJsonValue;
}
