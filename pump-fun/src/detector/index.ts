import type { Config } from '../config/schema.ts';
import type { TypedBus } from '../core/bus.ts';
import type { Repositories } from '../persistence/repositories.ts';
import type { RpcClient } from '../core/rpc.ts';
import type { FeedGraduation, GraduationEvent } from '../core/types.ts';
import type { SlotClock } from '../core/slotClock.ts';
import { logger } from '../core/logger.ts';
import { MintDedupe } from './dedupe.ts';
import { LatencyStats } from './latency.ts';
import { FeedWatchdog } from './watchdog.ts';
import { FeedCoverage } from './coverage.ts';
import type { DetectionFeed } from './feed.ts';
import { PumpPortalFeed } from './pumpportal.ts';
import { HeliusWsFeed } from './heliusWs.ts';
import { LaserstreamFeed } from './laserstream.ts';
import { PROGRAM_IDS } from '../core/constants.ts';
import { readSecret, heliusApiKeyFromUrl } from '../config/load.ts';

/**
 * Detection orchestrator (Section 4 / Phase 1). Wires every feed through
 * dedupe → on-chain confirmation → the event bus + decision log, tracks
 * detection latency, and debounces feed health into a single stream-health
 * signal the risk manager (Phase 5) will consume to gate new entries.
 */

const CONFIRM_MAX_ATTEMPTS = 6;
const CONFIRM_INTERVAL_MS = 500;
/** Watchdog + coverage evaluation cadence. */
const WATCHDOG_TICK_MS = 1_000;
/** How long after the first sighting a mint's feed coverage is evaluated (> PumpPortal relay lag). */
const COVERAGE_SETTLE_MS = 20_000;
/** Authority-rotation tripwire alert rate limit. */
const TRIPWIRE_ALERT_INTERVAL_MS = 60 * 60_000;
const PORTAL_FEED = 'pumpportal';
/** No slot feed live: poll getSlot at this cadence so the SlotClock still has a reading. */
const SLOT_POLL_MS = 2_000;

export interface DetectorDeps {
  config: Config;
  bus: TypedBus;
  repos: Repositories;
  /** Optional: without it, on-chain confirmation is skipped. */
  rpc?: RpcClient;
  /** Optional: chain-relative latency stamping (fed by the feeds' slot ticks). */
  slotClock?: SlotClock;
  /** Test hook: pre-built feeds instead of the config-driven ones. */
  feeds?: DetectionFeed[];
  now?: () => number;
}

export class Detector {
  private readonly config: Config;
  private readonly bus: TypedBus;
  private readonly repos: Repositories;
  private readonly rpc: RpcClient | undefined;
  private readonly slotClock: SlotClock | undefined;
  private readonly now: () => number;
  private readonly log = logger.child({ mod: 'detector' });

  private readonly dedupe: MintDedupe;
  private readonly latency = new LatencyStats();
  private readonly feeds: DetectionFeed[] = [];
  private readonly feedHealth = new Map<string, boolean>();
  private readonly watchdog: FeedWatchdog;
  private readonly coverage: FeedCoverage;
  private watchdogTimer: NodeJS.Timeout | null = null;
  private slotPollTimer: NodeJS.Timeout | null = null;
  /** Consecutive PumpPortal-only graduations while an on-chain feed was healthy. */
  private onChainMissStreak = 0;
  private lastTripwireAlertAtMs: number | null = null;
  /**
   * Every mint ever graduated, loaded from the DB at boot and grown as new
   * graduations land. Unlike `dedupe` (a short cross-feed TTL for the SAME
   * event arriving from multiple feeds within seconds), this is permanent and
   * survives restarts — a real bonding curve graduates once, so a repeat
   * "graduation" for a known mint (hours, days, or process restarts later) is
   * always spurious and is dropped before spending any enrichment budget.
   */
  private readonly seenMints: Set<string>;

  private streamDown = false;
  private graceTimer: NodeJS.Timeout | null = null;

  constructor(deps: DetectorDeps) {
    this.config = deps.config;
    this.bus = deps.bus;
    this.repos = deps.repos;
    this.rpc = deps.rpc;
    this.slotClock = deps.slotClock;
    this.now = deps.now ?? Date.now;
    this.dedupe = new MintDedupe(this.config.detector.dedupeTtlMs, this.now);
    this.seenMints = this.repos.listGraduatedMints();
    const liveness = this.config.detector.liveness;
    this.watchdog = new FeedWatchdog({
      slotSilenceMs: liveness.slotSilenceMs,
      portalSilenceMs: liveness.portalSilenceMs,
      portalMissedGraduations: liveness.portalMissedGraduations,
      reconnectMaxMs: this.config.detector.reconnectMaxMs,
      now: this.now,
    });
    this.coverage = new FeedCoverage({ settleMs: COVERAGE_SETTLE_MS, now: this.now });
    this.feeds = deps.feeds ?? this.buildFeeds();
  }

  private buildFeeds(): DetectionFeed[] {
    const d = this.config.detector;
    const feeds: DetectionFeed[] = [];
    if (d.pumpportalEnabled) {
      feeds.push(
        new PumpPortalFeed({
          url: this.config.rpc?.pumpportalWs ?? 'wss://pumpportal.fun/api/data',
          reconnectBaseMs: d.reconnectBaseMs,
          reconnectMaxMs: d.reconnectMaxMs,
        }),
      );
    }
    if (d.heliusWsEnabled) {
      if (this.rpc && this.config.rpc?.primaryHttp) {
        feeds.push(
          new HeliusWsFeed({
            rpc: this.rpc,
            httpUrl: this.config.rpc.primaryHttp,
            pumpFunProgramId: this.config.programs.pumpFun ?? PROGRAM_IDS.PUMP_FUN,
            reconnectBaseMs: d.reconnectBaseMs,
            reconnectMaxMs: d.reconnectMaxMs,
            atlasEnabled: d.heliusAtlasEnabled,
            migrationAuthority: d.migrationAuthority || undefined,
          }),
        );
      } else {
        this.log.warn('detector.heliusWsEnabled but no rpc.primaryHttp — Helius WS feed skipped');
      }
    }
    if (d.laserstreamEnabled) {
      if (this.rpc && this.config.rpc?.primaryGrpc) {
        const token =
          (this.config.rpc.primaryGrpcTokenEnvVar ? readSecret(this.config.rpc.primaryGrpcTokenEnvVar) : undefined) ??
          heliusApiKeyFromUrl(this.config.rpc.primaryHttp);
        feeds.push(
          new LaserstreamFeed({
            endpoint: this.config.rpc.primaryGrpc,
            ...(token ? { token } : {}),
            rpc: this.rpc,
            pumpFunProgramId: this.config.programs.pumpFun ?? PROGRAM_IDS.PUMP_FUN,
            migrationAuthority: d.migrationAuthority || undefined,
            reconnectBaseMs: d.reconnectBaseMs,
            reconnectMaxMs: d.reconnectMaxMs,
          }),
        );
      } else {
        this.log.warn('detector.laserstreamEnabled but no rpc.primaryGrpc / rpc client — LaserStream feed skipped');
      }
    }
    return feeds;
  }

  async start(): Promise<void> {
    if (this.rpc) {
      const ok = await this.rpc.getHealth();
      this.log.info('rpc health check', { healthy: ok });
    } else if (this.config.detector.confirmOnChain) {
      this.log.warn('confirmOnChain enabled but no rpc.primaryHttp configured — graduations will be recorded unconfirmed');
    }

    for (const feed of this.feeds) {
      this.feedHealth.set(feed.name, false);
      this.watchdog.register(feed.name, () => feed.liveness);
      feed.onGraduation((g) => void this.onFeedGraduation(g));
      feed.onHealth((healthy, detail) => {
        if (healthy) this.watchdog.markConnected(feed.name, this.now());
        this.onFeedHealth(feed.name, healthy, detail);
      });
      feed.onActivity((a) => {
        this.watchdog.touch(feed.name, a.atMs);
        if (a.slot !== undefined) this.slotClock?.observe(a.slot, feed.name);
      });
      feed.start();
    }
    this.watchdogTimer = setInterval(() => this.watchdogTick(), WATCHDOG_TICK_MS);
    this.watchdogTimer.unref?.();
    // Without a slot-subscribed feed the SlotClock would stay empty; keep it
    // populated with a cheap getSlot poll so detection/landing samples still
    // get stamped (2 s granularity instead of 400 ms).
    if (this.slotClock && this.rpc && !this.feeds.some((f) => f.liveness === 'slot')) {
      const clock = this.slotClock;
      this.slotPollTimer = setInterval(() => void clock.current(), SLOT_POLL_MS);
      this.slotPollTimer.unref?.();
    }
    this.log.info('detector started', {
      feeds: this.feeds.map((f) => f.name),
      confirmOnChain: this.config.detector.confirmOnChain && Boolean(this.rpc),
      migrationAuthority: this.config.detector.migrationAuthority || 'none (firehose)',
      liveness: this.config.detector.liveness,
    });
  }

  async stop(): Promise<void> {
    if (this.graceTimer) clearTimeout(this.graceTimer);
    if (this.watchdogTimer) clearInterval(this.watchdogTimer);
    if (this.slotPollTimer) clearInterval(this.slotPollTimer);
    this.watchdogTimer = null;
    this.slotPollTimer = null;
    await Promise.all(this.feeds.map((f) => f.stop()));
  }

  /** Feeds silent past their bound → forced reconnect; then cross-feed coverage rules. */
  private watchdogTick(): void {
    for (const d of this.watchdog.tick(this.now())) {
      const feed = this.feeds.find((f) => f.name === d.feed);
      if (!feed) continue;
      this.log.warn('feed watchdog: forcing reconnect', { feed: d.feed, silentMs: d.silentMs, reason: d.reason });
      this.bus.emit('alert', {
        level: 'warn',
        message: `♻️ ${d.feed} silent ${Math.round(d.silentMs / 1000)}s — reconnecting`,
      });
      feed.reconnect(`watchdog: ${d.reason}`);
      this.watchdog.markReconnecting(d.feed);
    }
    for (const settled of this.coverage.settle(this.now())) this.applyCoverage(settled.feeds, settled.mint);
  }

  /**
   * Cross-feed coverage of one settled graduation.
   *  A) On-chain feeds saw it, PumpPortal (healthy) did not → PumpPortal is
   *     probably half-open: reconnect after N consecutive misses.
   *  B) Only PumpPortal saw it while an on-chain feed was healthy → the
   *     on-chain feeds are blind (migration authority rotated? log format
   *     changed?): alert, never reconnect.
   */
  private applyCoverage(feeds: ReadonlySet<string>, mint: string): void {
    const onChainFeeds = this.feeds.filter((f) => f.name !== PORTAL_FEED).map((f) => f.name);
    const hasPortalFeed = this.feeds.some((f) => f.name === PORTAL_FEED);
    const seenOnChain = onChainFeeds.some((n) => feeds.has(n));
    const seenPortal = feeds.has(PORTAL_FEED);
    const onChainHealthy = onChainFeeds.some((n) => this.feedHealth.get(n) === true);

    if (hasPortalFeed && onChainFeeds.length > 0) {
      if (seenPortal) {
        this.watchdog.portalHit();
      } else if (seenOnChain && this.feedHealth.get(PORTAL_FEED) === true && this.watchdog.portalMissed()) {
        const portal = this.feeds.find((f) => f.name === PORTAL_FEED);
        const reason = `missed ${this.config.detector.liveness.portalMissedGraduations} consecutive graduations seen on-chain`;
        this.log.warn('feed coverage: pumpportal blind — forcing reconnect', { mint: short(mint), reason });
        portal?.reconnect(`coverage: ${reason}`);
        this.watchdog.markReconnecting(PORTAL_FEED);
      }
    }

    if (onChainFeeds.length > 0) {
      if (seenOnChain) {
        this.onChainMissStreak = 0;
      } else if (seenPortal && onChainHealthy) {
        this.onChainMissStreak++;
        const threshold = this.config.detector.liveness.onChainMissedGraduations;
        const rateLimited =
          this.lastTripwireAlertAtMs !== null && this.now() - this.lastTripwireAlertAtMs < TRIPWIRE_ALERT_INTERVAL_MS;
        if (this.onChainMissStreak >= threshold && !rateLimited) {
          this.lastTripwireAlertAtMs = this.now();
          const authority = this.config.detector.migrationAuthority || 'none';
          const message = `⚠️ on-chain feeds saw 0 of the last ${this.onChainMissStreak} migrations PumpPortal delivered — detector.migrationAuthority (${authority}) may have rotated; detection is PumpPortal-only`;
          this.log.error('feed coverage tripwire', { streak: this.onChainMissStreak, authority });
          this.bus.emit('alert', { level: 'error', message, telegram: true });
        }
      }
    }
  }

  private async onFeedGraduation(g: FeedGraduation): Promise<void> {
    // Coverage sees every delivery (incl. second-feed duplicates inside the
    // dedupe window) — that is what makes cross-feed comparison possible.
    if (!this.seenMints.has(g.mint) || this.dedupe.peek(g.mint)) {
      this.coverage.observe(g.mint, g.feedSource);
    }
    if (this.seenMints.has(g.mint)) {
      this.log.debug('mint already graduated previously — dropping repeat detection', {
        mint: g.mint,
        feed: g.feedSource,
      });
      return;
    }
    if (!this.dedupe.firstSeen(g.mint)) {
      this.log.debug('duplicate graduation dropped', { mint: g.mint, feed: g.feedSource });
      return;
    }

    // Chain-relative stamp: read the clock BEFORE feeding it this tx's slot so
    // a −1 lag (tx frame beat the slot frame) stays visible.
    const receivedSlot = this.slotClock?.get()?.slot;
    if (g.slot !== undefined) this.slotClock?.observe(g.slot, g.feedSource);
    const maxStale = this.config.detector.maxStaleSlots;
    if (maxStale > 0 && g.slot !== undefined && receivedSlot !== undefined && receivedSlot - g.slot > maxStale) {
      this.log.warn('stale migration dropped (feed replay?)', {
        mint: g.mint,
        feed: g.feedSource,
        slot: g.slot,
        receivedSlot,
        behind: receivedSlot - g.slot,
      });
      return;
    }
    this.seenMints.add(g.mint);

    // On-chain confirmation used to block here (~550-600ms typical, up to 3s)
    // before screening could even start. It never actually gated anything
    // downstream — `confirm.confirmed` was computed and logged but nothing
    // checked it before emitting `graduation` or opening a position — so
    // moving it off the critical path is a pure latency win, not a safety
    // change: behavior (screen/buy regardless of confirm status) is
    // unchanged, only the timing of the log/DB record shifts later.
    const latencyMs = Number(process.hrtime.bigint() - g.receivedAtNs) / 1e6;
    this.latency.add(latencyMs);

    const event: GraduationEvent = {
      mint: g.mint,
      venue: g.venue ?? 'pumpswap',
      poolAddress: g.poolAddress ?? '',
      slot: g.slot ?? 0,
      feedSource: g.feedSource,
      receivedAtNs: g.receivedAtNs,
      detectionLatencyMs: latencyMs,
      ...(receivedSlot !== undefined ? { receivedSlot } : {}),
    };

    this.bus.emit('graduation', event);

    this.log.info('graduation detected', {
      mint: g.mint,
      venue: event.venue,
      feed: g.feedSource,
      slot: event.slot || undefined,
      receivedSlot,
      slotLag: receivedSlot !== undefined && g.slot !== undefined ? receivedSlot - g.slot : undefined,
      latencyMs: Math.round(latencyMs),
    });

    if (this.latency.count % this.config.detector.latencyLogEveryN === 0) {
      this.log.info('detection latency stats', this.latency.summary());
    }

    this.bus.emit('alert', {
      level: 'info',
      message: `🎓 graduation ${short(g.mint)} via ${event.venue} (${g.feedSource})`,
    });

    // Confirmation + persistence run in the background from here — same
    // getTransaction check as before, just no longer blocking the emit above.
    void this.confirmAndPersist(g, event, latencyMs);
  }

  /**
   * Verify the migration landed on-chain and persist the graduation record.
   * Runs after `graduation` has already been emitted (see onFeedGraduation) —
   * purely for DB accuracy and an unconfirmed-tx warning, same as the old
   * inline confirm() step's actual effect (it never gated screening/buying).
   */
  private async confirmAndPersist(g: FeedGraduation, event: GraduationEvent, latencyMs: number): Promise<void> {
    const confirm = await this.confirm(g);
    const persisted: GraduationEvent = confirm.slot !== undefined ? { ...event, slot: confirm.slot } : event;
    // Chain-relative detection lag: slot clock at receipt vs the migration's
    // slot (feed-provided, else the confirmed tx's). Off the hot path, so a
    // getSlot fallback is acceptable when no slot feed was live.
    const migrationSlot = g.slot ?? confirm.slot;
    const received = event.receivedSlot ?? (migrationSlot !== undefined ? await this.slotClock?.current() : undefined);
    try {
      this.repos.recordGraduation(persisted);
      this.repos.recordLatencySample({
        kind: 'detection',
        latencyMs,
        mint: g.mint,
        feedSource: g.feedSource,
      });
      if (received !== undefined && migrationSlot !== undefined) {
        this.repos.recordLatencySample({
          kind: 'detection_slots',
          latencyMs: received - migrationSlot,
          mint: g.mint,
          feedSource: g.feedSource,
        });
      }
    } catch (err) {
      this.log.error('failed to persist graduation', { mint: g.mint, err });
    }
    if (!confirm.confirmed) {
      this.log.warn('migration tx did not confirm on-chain', { mint: g.mint, slot: confirm.slot });
    }
  }

  /**
   * Confirm the migration landed on-chain (no tx error). Retries briefly since
   * `confirmed` commitment lags the WS signal. Returns confirmed=false (rather
   * than throwing) when unavailable so detection is still logged.
   */
  private async confirm(g: FeedGraduation): Promise<{ confirmed: boolean; slot?: number }> {
    if (!this.rpc || !this.config.detector.confirmOnChain || !g.signature) {
      return { confirmed: false };
    }
    for (let attempt = 0; attempt < CONFIRM_MAX_ATTEMPTS; attempt++) {
      try {
        const tx = await this.rpc.getTransactionConfirmation(g.signature);
        if (tx) {
          if (tx.err) {
            this.log.warn('migration tx failed on-chain', { mint: g.mint, err: tx.err });
            return { confirmed: false, slot: tx.slot };
          }
          return { confirmed: true, slot: tx.slot };
        }
      } catch (err) {
        this.log.debug('confirm attempt errored', { mint: g.mint, attempt, err });
      }
      await delay(CONFIRM_INTERVAL_MS);
    }
    this.log.warn('graduation not confirmed within budget', { mint: g.mint });
    return { confirmed: false };
  }

  private onFeedHealth(feed: string, healthy: boolean, detail?: string): void {
    this.feedHealth.set(feed, healthy);
    const anyHealthy = [...this.feedHealth.values()].some(Boolean);

    if (anyHealthy) {
      if (this.graceTimer) {
        clearTimeout(this.graceTimer);
        this.graceTimer = null;
      }
      if (this.streamDown) {
        this.streamDown = false;
        this.bus.emit('streamHealth', { source: 'detector', healthy: true });
        this.bus.emit('alert', { level: 'info', message: '✅ detection feed recovered' });
        this.log.info('detection feeds healthy again');
      }
      return;
    }

    // All feeds down — wait out the grace window before declaring an outage
    // (Section 4.2 / Section 8: alert if down > streamDownGraceMs).
    if (!this.streamDown && !this.graceTimer) {
      this.graceTimer = setTimeout(() => {
        this.streamDown = true;
        this.graceTimer = null;
        this.bus.emit('streamHealth', {
          source: 'detector',
          healthy: false,
          ...(detail ? { detail } : {}),
        });
        this.bus.emit('alert', {
          level: 'warn',
          message: `⚠️ detection feed down >${Math.round(this.config.risk.streamDownGraceMs / 1000)}s — new entries paused`,
        });
        this.log.warn('all detection feeds down past grace window', { detail });
      }, this.config.risk.streamDownGraceMs);
    }
  }
}

function delay(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function short(mint: string): string {
  return mint.length > 10 ? `${mint.slice(0, 4)}…${mint.slice(-4)}` : mint;
}
