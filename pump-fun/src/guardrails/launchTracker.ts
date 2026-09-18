import type { Mint } from '../core/types.ts';
import type { Repositories } from '../persistence/repositories.ts';
import type { RpcClient } from '../core/rpc.ts';
import { logger } from '../core/logger.ts';
import { getActiveRunSession } from '../core/session.ts';
import {
  deriveBondingCurvePda,
  decodeBondingCurve,
  curvePriceSol,
} from '../enrichment/curve.ts';

export interface LaunchTrackerOptions {
  /** Poll cadence (ms). Default 10 s — launch paper stats need no urgency. */
  pollMs?: number;
  /** Max curves tracked at once. Excess launches are skipped (counted). */
  maxConcurrent?: number;
  /** How long to track each mint (ms). Default 2 h — covers the graduation window. */
  windowMs?: number;
  now?: () => number;
}

interface OpenTrack {
  curveAddress: string;
  baselinePrice: number | null;
  peakPrice: number | null;
  startedMs: number;
  samples: number;
}

/**
 * Capital-free paper tracking of pre-graduation launches (S1).
 *
 * Adopts recent launches newest-first up to `maxConcurrent` (one batched
 * account read per tick ≈ 0.1 rps at defaults), records the curve mid from
 * creation, and closes each track graduated (mint hit `graduations`, or the
 * curve's complete flag) or expired at `windowMs`. Outcomes land in
 * `launch_tracks`: baseline/peak price, peak MFE, graduation flag + time.
 *
 * Never touches positions, candidates, screening, or risk — the only shared
 * surface is the `launches` + `graduations` tables it reads.
 */
export class LaunchTracker {
  private readonly rpc: RpcClient;
  private readonly repos: Repositories;
  private readonly pollMs: number;
  private readonly maxConcurrent: number;
  private readonly windowMs: number;
  private readonly now: () => number;
  private readonly log = logger.child({ mod: 'launch-track' });
  private readonly states = new Map<Mint, OpenTrack>();
  private timer: NodeJS.Timeout | null = null;
  private skippedAtCapacity = 0;

  constructor(rpc: RpcClient, repos: Repositories, opts: LaunchTrackerOptions = {}) {
    this.rpc = rpc;
    this.repos = repos;
    this.pollMs = opts.pollMs ?? 10_000;
    this.maxConcurrent = opts.maxConcurrent ?? 10;
    this.windowMs = opts.windowMs ?? 2 * 60 * 60_000;
    this.now = opts.now ?? (() => Date.now());
  }

  start(): void {
    if (this.timer) return;
    this.timer = setInterval(() => void this.tick().catch((err) => this.log.error('tick failed', { err })), this.pollMs);
    this.timer.unref?.();
  }

  stop(): void {
    if (this.timer) clearInterval(this.timer);
    this.timer = null;
    this.states.clear();
  }

  get size(): number {
    return this.states.size;
  }

  get skipped(): number {
    return this.skippedAtCapacity;
  }

  /** Adopt + poll one round. Public for tests; the timer calls it live. */
  async tick(): Promise<void> {
    this.adopt();
    await this.poll();
  }

  private adopt(): void {
    const free = this.maxConcurrent - this.states.size;
    if (free <= 0) return;
    // Newest-first: at ~17 launches/min the backlog never drains, so paper
    // stats cover current flow rather than hours-stale launches.
    const fresh = this.repos.listUntrackedLaunches(free);
    for (const mint of fresh) {
      const curveAddress = deriveBondingCurvePda(mint);
      if (!curveAddress) continue;
      this.states.set(mint, {
        curveAddress,
        baselinePrice: null,
        peakPrice: null,
        startedMs: this.now(),
        samples: 0,
      });
      try {
        this.repos.openLaunchTrack(mint);
      } catch (err) {
        this.log.error('failed to open launch track', { mint, err });
        this.states.delete(mint);
      }
    }
    const backlog = this.repos.countUntrackedLaunches();
    if (backlog > 0) {
      this.skippedAtCapacity += backlog;
      this.log.debug('launch tracks at capacity — skipping backlog', { cap: this.maxConcurrent, backlog });
    }
  }

  private async poll(): Promise<void> {
    if (this.states.size === 0) return;
    const mints = [...this.states.keys()];
    const pdas = mints.map((m) => this.states.get(m)!.curveAddress);
    let accounts: Array<{ data: string; owner: string } | null>;
    try {
      accounts = await this.rpc.getMultipleAccountsBase64(pdas, 'processed');
    } catch (err) {
      this.log.warn('curve batch read failed', { err });
      return;
    }
    const session = getActiveRunSession();
    for (let i = 0; i < mints.length; i++) {
      const mint = mints[i]!;
      const st = this.states.get(mint);
      if (!st) continue;
      const acct = accounts[i];
      const decoded = acct ? decodeBondingCurve(acct.data, acct.owner, st.curveAddress) : null;
      const price = decoded ? curvePriceSol(decoded) : null;
      const graduated = decoded?.complete === true || this.repos.isGraduated(mint);
      if (price !== null) {
        if (st.baselinePrice === null) {
          st.baselinePrice = price;
          st.peakPrice = price;
          try {
            this.repos.setLaunchBaseline(mint, price);
          } catch (err) {
            this.log.error('failed to set launch baseline', { mint, err });
          }
        } else if (st.peakPrice === null || price > st.peakPrice) {
          st.peakPrice = price;
        }
        st.samples++;
        try {
          this.repos.updateLaunchTrack(mint, st.baselinePrice, st.peakPrice, st.samples);
        } catch (err) {
          this.log.error('failed to update launch track', { mint, err });
        }
      }
      const expired = this.now() - st.startedMs >= this.windowMs;
      // An unpriced (brand-new, zero-reserve) curve that graduates is still a
      // graduation: close on the graduated flag regardless of baseline.
      if (graduated || expired) {
        this.close(mint, st, graduated, session?.id ?? null, session?.configHash ?? null);
      }
    }
  }

  private close(
    mint: string,
    st: OpenTrack,
    graduated: boolean,
    sessionId: number | null,
    configHash: string | null,
  ): void {
    this.states.delete(mint);
    const peakMfePct =
      st.baselinePrice !== null && st.peakPrice !== null && st.baselinePrice > 0
        ? (st.peakPrice / st.baselinePrice - 1) * 100
        : null;
    try {
      this.repos.closeLaunchTrack({
        mint,
        baselinePrice: st.baselinePrice,
        peakPrice: st.peakPrice,
        peakMfePct,
        graduated,
        trackedMs: this.now() - st.startedMs,
        samples: st.samples,
        sessionId,
        configHash,
      });
    } catch (err) {
      this.log.error('failed to close launch track', { mint, err });
    }
    this.log.info('launch track closed', { mint, graduated, peakMfePct, samples: st.samples });
  }
}
