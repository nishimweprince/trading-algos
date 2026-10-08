import { loadConfig, ConfigError } from './config/load.ts';
import { acquireLock, LockError } from './core/lock.ts';
import { TypedBus } from './core/bus.ts';
import { logger } from './core/logger.ts';
import { RpcClient, createFailoverFetch } from './core/rpc.ts';
import { installKeepAliveDispatcher } from './core/http.ts';
import { setActiveRunSession } from './core/session.ts';
import type { CandidateVerdict, CheckResult, GraduationEvent } from './core/types.ts';
import { openDb } from './persistence/db.ts';
import { Repositories } from './persistence/repositories.ts';
import { GuardrailPipeline } from './guardrails/pipeline.ts';
import { ShadowTracker } from './guardrails/shadow.ts';
import { ClusterWarmer } from './enrichment/features/clusterWarm.ts';
import { FeeModel, sdkFeeTierLoader } from './positions/feeModel.ts';
import { Simulator } from './positions/simulator.ts';
import { createDecisionClient } from './decision/index.ts';
import { EntryDecider } from './decision/entryDecider.ts';
import { reviveEnrichment } from './research/screenReplay.ts';
import type { Candidate } from './enrichment/types.ts';

/**
 * Research worker (PUMPDESK_ROLE=research; feat/fra-lean WS4c).
 *
 * Everything that reads chain history for analysis — background enrichment
 * (holders, DAS, early flow), manipulation features, the creator funding
 * cluster warm-up (H13's cache), shadow dry-runs of vetoes, confirm arms and
 * the decision model's shadow call — runs here, on the research Helius key,
 * against the trader's SQLite file. The trader (role=trader) keeps only
 * detection, the fast screen, execution and exits on its event loop.
 *
 * Feeds: tails `launches` (creators → ClusterWarmer, whose results the
 * trader's H13 reads from the DB) and `candidates` (each recorded verdict →
 * GuardrailPipeline.researchRecorded). Starts from the current tail: history
 * is not re-processed on restart.
 */

const log = logger.child({ mod: 'research-worker' });
const TAIL_MS = 500;
const MAX_IN_FLIGHT = 4;

async function main(): Promise<void> {
  installKeepAliveDispatcher();
  const config = loadConfig(process.env.CONFIG_PATH ? { path: process.env.CONFIG_PATH } : {});
  const lock = acquireLock(process.env.LOCK_PATH ?? `${config.persistence.dbPath}.research.lock`);
  // The worker can afford to wait for the trader's writes.
  const db = openDb({ path: config.persistence.dbPath, busyTimeoutMs: 2_000 });
  const repos = new Repositories(db);
  const bus = new TypedBus();

  const session = db
    .prepare(`SELECT id, config_hash AS configHash, mode FROM run_sessions ORDER BY id DESC LIMIT 1`)
    .get() as { id: number; configHash: string; mode: typeof config.mode } | undefined;
  if (session) setActiveRunSession({ id: session.id, configHash: session.configHash, mode: session.mode });

  const httpUrl = config.rpc?.enrichmentHttp?.trim() || config.rpc?.primaryHttp;
  if (!httpUrl) throw new ConfigError('research worker needs rpc.enrichmentHttp (or rpc.primaryHttp)');
  const rpc = new RpcClient({
    httpUrl,
    fallbackHttpUrls: (config.rpc?.fallbackHttp ?? []).filter((u) => u !== httpUrl),
    maxConcurrent: config.rpc?.maxConcurrentRequests ?? 12,
    timeoutMs: config.rpc?.readTimeoutMs ?? 900,
    maxRps: config.rpc?.researchMaxRps ?? 0,
  });

  const feeModel = FeeModel.fromConfig(
    config.fees,
    config.fees.feeModel === 'tiered'
      ? sdkFeeTierLoader(httpUrl, createFailoverFetch([httpUrl], { timeoutMs: config.rpc?.readTimeoutMs ?? 900 }))
      : undefined,
  );
  void feeModel.start();

  const shadow = config.shadow.enabled
    ? new ShadowTracker(rpc, repos, {
        windowMs: config.shadow.windowMinutes * 60_000,
        pollMs: config.shadow.pollMs,
        maxConcurrent: config.shadow.maxConcurrent,
        sizeSol: config.shadow.sizeSol ?? config.entry.minAbsoluteSol,
        exits: config.exits,
        fees: config.fees,
        maxSlippagePct: config.entry.maxSlippagePct,
        feeModel,
        simulator: new Simulator({ ...config.simulator, seed: config.simulator.seed + 2 }),
        recordPaths: config.shadow.recordPaths,
      })
    : undefined;

  const decisionClient = config.decision.mode === 'shadow' ? createDecisionClient(config) : null;
  const decision = decisionClient ? new EntryDecider({ config, repos, client: decisionClient }) : undefined;

  const pipeline = new GuardrailPipeline({
    config,
    bus,
    repos,
    rpc,
    ...(shadow ? { shadow } : {}),
    ...(decision ? { decision } : {}),
    researchInProcess: true,
  });

  const features = config.guardrails.features;
  const clusterWarmer =
    config.guardrails.clusterWarm.enabled && features.enabled && features.cluster.enabled
      ? new ClusterWarmer(rpc, repos, {
          hops: features.cluster.hops,
          maxSigs: features.cluster.maxSigs,
          maxConcurrent: config.guardrails.clusterWarm.maxConcurrent,
          maxQueue: config.guardrails.clusterWarm.maxQueue,
        })
      : null;

  shadow?.start();
  let launchCursor = repos.maxRowid('launches');
  let candidateCursor = repos.maxRowid('candidates');
  let inFlight = 0;
  const queue: Array<() => Promise<void>> = [];
  const pump = () => {
    while (inFlight < MAX_IN_FLIGHT && queue.length) {
      const job = queue.shift()!;
      inFlight++;
      void job().finally(() => {
        inFlight--;
        pump();
      });
    }
  };

  const tick = () => {
    try {
      if (clusterWarmer) {
        for (const l of repos.launchesAfter(launchCursor)) {
          launchCursor = l.rowid;
          if (l.creator) clusterWarmer.enqueue(l.creator);
        }
      }
      for (const row of repos.candidatesAfter(candidateCursor)) {
        candidateCursor = row.rowid;
        const built = candidateFromRecorded(row);
        if (!built) continue;
        queue.push(() =>
          pipeline.researchRecorded(built.candidate, built.verdict).catch((err) => log.warn('research failed', { mint: row.mint, err })),
        );
      }
      pump();
    } catch (err) {
      log.warn('research tail failed', { err });
    }
  };
  const timer = setInterval(tick, TAIL_MS);
  log.info('research worker started', { launchCursor, candidateCursor, shadow: Boolean(shadow), clusterWarm: Boolean(clusterWarmer) });

  const stop = (signal: string) => {
    log.info('research worker stopping', { signal });
    clearInterval(timer);
    shadow?.stop();
    clusterWarmer?.stop();
    db.close();
    lock.release();
    process.exit(0);
  };
  process.on('SIGINT', () => stop('SIGINT'));
  process.on('SIGTERM', () => stop('SIGTERM'));
}

/** Rebuild the candidate + verdict the trader recorded. Null when the row lacks enrichment. */
export function candidateFromRecorded(row: ReturnType<Repositories['candidatesAfter']>[number]): { candidate: Candidate; verdict: CandidateVerdict } | null {
  if (!row.enrichmentJson) return null;
  const parse = <T>(json: string | null, fallback: T): T => {
    try {
      return json ? (JSON.parse(json) as T) : fallback;
    } catch {
      return fallback;
    }
  };
  const detectedAtMs = row.detectedAt ? Date.parse(row.detectedAt.includes('T') ? row.detectedAt : `${row.detectedAt.replace(' ', 'T')}Z`) : NaN;
  const graduation: GraduationEvent = {
    mint: row.mint,
    venue: 'pumpswap',
    poolAddress: '',
    slot: row.slot ?? 0,
    feedSource: (row.feedSource ?? 'laserstream') as GraduationEvent['feedSource'],
    receivedAtNs: 0n,
    ...(Number.isFinite(detectedAtMs) ? { detectedAtMs } : {}),
  };
  const enrichment = reviveEnrichment(row.enrichmentJson);
  const verdict: CandidateVerdict = {
    mint: row.mint,
    verdict: row.verdict === 'accept' ? 'accept' : 'veto',
    hardChecks: parse<CheckResult[]>(row.hardCheckResults, []),
    softScore: row.softScore ?? 0,
    vetoReasons: parse<string[]>(row.vetoReasons, []),
    highVolatility: row.highVolatility === 1,
    sizeMultiplier: row.sizeMultiplier ?? 0,
  };
  return { candidate: { graduation, enrichment }, verdict };
}

if (import.meta.url === `file://${process.argv[1]}`) {
  main().catch((err) => {
    if (err instanceof LockError) log.error('another research worker holds the lock', { err: err.message });
    else log.error('research worker failed to start', { err });
    process.exit(1);
  });
}
