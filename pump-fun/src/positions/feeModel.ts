import { logger } from '../core/logger.ts';
import { LAMPORTS_PER_SOL } from '../core/constants.ts';
import {
  PUMP_TOTAL_SUPPLY_WHOLE,
  PUMPSWAP_FEE_TIERS,
  tierForMcap,
  totalBps,
  type FeeTierBps,
} from './feeTiers.ts';

/**
 * Paper-side PumpSwap fee model (work plan 2026-09-25 P1.1, F4).
 *
 * Tier selection mirrors the SDK (`calculateFeeTier`, which live quoting uses
 * inside buyQuoteInput / sellBaseInput), and the tier TABLE comes from the
 * on-chain pump-fees `FeeConfig` when it has been fetched, so paper and live
 * charge the same schedule. Until then — or offline — the documented schedule
 * in feeTiers.ts. `mode: 'flat'` is the legacy swapFeePct and logs a warning
 * once: it under-charges graduations ~5x.
 *
 * Per-pool creator-fee overrides (`Pool.creatorFeeBps` behind
 * `creatorFeeConfigurable`) are not modelled: paper has no pool account, and
 * the override only ever applies above the schedule's first tier.
 */
export type FeeTierSource = 'onchain' | 'static' | 'flat';

export interface FeeQuote {
  bps: number;
  mcapSol: number | null;
  source: FeeTierSource;
}

export type FeeTierLoader = () => Promise<FeeTierBps[]>;

export class FeeModel {
  private tiers: readonly FeeTierBps[] = PUMPSWAP_FEE_TIERS;
  private source: FeeTierSource;
  private readonly flatBps: number;
  private readonly loader: FeeTierLoader | undefined;
  private readonly refreshMs: number;
  private timer: ReturnType<typeof setInterval> | null = null;
  private warnedFlat = false;
  private readonly log = logger.child({ mod: 'fee-model' });

  constructor(opts: {
    mode: 'tiered' | 'flat';
    swapFeePct: number;
    loader?: FeeTierLoader;
    refreshMs?: number;
  }) {
    this.source = opts.mode === 'flat' ? 'flat' : 'static';
    this.flatBps = opts.swapFeePct * 100;
    this.loader = opts.mode === 'tiered' ? opts.loader : undefined;
    this.refreshMs = opts.refreshMs ?? 600_000;
  }

  static fromConfig(fees: { feeModel?: 'tiered' | 'flat'; swapFeePct: number; feeConfigRefreshMs?: number }, loader?: FeeTierLoader): FeeModel {
    return new FeeModel({
      mode: fees.feeModel ?? 'tiered',
      swapFeePct: fees.swapFeePct,
      ...(loader ? { loader } : {}),
      ...(fees.feeConfigRefreshMs !== undefined ? { refreshMs: fees.feeConfigRefreshMs } : {}),
    });
  }

  /** Fetch the on-chain table now and every refreshMs. Never throws. */
  async start(): Promise<void> {
    if (!this.loader) return;
    await this.refresh();
    this.timer = setInterval(() => void this.refresh(), this.refreshMs);
    this.timer.unref?.();
  }

  stop(): void {
    if (this.timer) clearInterval(this.timer);
    this.timer = null;
  }

  async refresh(): Promise<void> {
    if (!this.loader) return;
    try {
      const tiers = await this.loader();
      if (tiers.length) {
        this.tiers = [...tiers].sort((a, b) => a.mcapSol - b.mcapSol);
        this.source = 'onchain';
        this.log.info('pumpswap fee tiers loaded', { tiers: tiers.length, firstBps: totalBps(this.tiers[0]!) });
      }
    } catch (err) {
      this.log.warn('pumpswap fee config fetch failed — using documented schedule', { err });
    }
  }

  get tierSource(): FeeTierSource {
    return this.source;
  }

  /** Fee for a leg at a known market cap (SOL). */
  forMcap(mcapSol: number | null): FeeQuote {
    if (this.source === 'flat') {
      if (!this.warnedFlat) {
        this.warnedFlat = true;
        this.log.warn('flat swapFeePct fee model in use — paper fees under-state PumpSwap tiers', { bps: this.flatBps });
      }
      return { bps: this.flatBps, mcapSol, source: 'flat' };
    }
    return { bps: totalBps(tierForMcap(mcapSol, this.tiers)), mcapSol, source: this.source };
  }

  /** Fee for a leg at a price (SOL per whole token); pump mints have a 1e9 supply. */
  forPrice(priceSolPerToken: number, supplyWhole = PUMP_TOTAL_SUPPLY_WHOLE): FeeQuote {
    const mcap = priceSolPerToken > 0 && Number.isFinite(priceSolPerToken) ? priceSolPerToken * supplyWhole : null;
    return this.forMcap(mcap);
  }
}

/**
 * Loader over the pump-swap SDK's `fetchFeeConfigAccount` (pump-fees
 * FeeConfig PDA). Imported lazily so paper/test boots never pull web3.
 */
export function sdkFeeTierLoader(httpUrl: string, fetchImpl?: typeof fetch): FeeTierLoader {
  return async () => {
    const [{ Connection }, { OnlinePumpAmmSdk }] = await Promise.all([
      import('@solana/web3.js'),
      import('@pump-fun/pump-swap-sdk'),
    ]);
    const connection = new Connection(httpUrl, { commitment: 'confirmed', ...(fetchImpl ? { fetch: fetchImpl } : {}) });
    const sdk = new OnlinePumpAmmSdk(connection);
    const cfg = await sdk.fetchFeeConfigAccount();
    return cfg.feeTiers.map((t) => ({
      mcapSol: Number(t.marketCapLamportsThreshold.toString()) / LAMPORTS_PER_SOL,
      creatorBps: Number(t.fees.creatorFeeBps.toString()),
      protocolBps: Number(t.fees.protocolFeeBps.toString()),
      lpBps: Number(t.fees.lpFeeBps.toString()),
    }));
  };
}
