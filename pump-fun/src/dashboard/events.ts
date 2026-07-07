import type { TypedBus } from '../core/bus.ts';
import type { Repositories, OperatorEventInput } from '../persistence/repositories.ts';
import type { DashboardEvent } from './queries.ts';

export interface DashboardEventPublisher {
  publish(event: DashboardEvent): void;
}

export function attachOperatorEventRecorder(
  bus: TypedBus,
  repos: Repositories,
  publisher?: DashboardEventPublisher,
): () => void {
  const unsubs = [
    bus.on('graduation', (g) =>
      record(repos, publisher, {
        category: 'graduation',
        level: 'info',
        message: `Graduation detected for ${short(g.mint)} on ${g.venue}`,
        entityMint: g.mint,
        payload: g,
      }),
    ),
    bus.on('verdict', (v) =>
      record(repos, publisher, {
        category: 'verdict',
        level: v.verdict === 'accept' ? 'info' : 'warn',
        message:
          v.verdict === 'accept'
            ? `Accepted ${short(v.mint)} at score ${v.softScore}`
            : `Vetoed ${short(v.mint)}: ${v.vetoReasons.join(', ') || 'guardrail'}`,
        entityMint: v.mint,
        payload: v,
      }),
    ),
    bus.on('entryVetoed', (e) =>
      record(repos, publisher, {
        category: 'entry',
        level: 'warn',
        message: `Entry blocked for ${short(e.mint)}: ${e.reason}${e.detail ? ` (${e.detail})` : ''}`,
        entityMint: e.mint,
        payload: e,
      }),
    ),
    bus.on('positionUpdate', (p) =>
      record(repos, publisher, {
        category: 'position',
        level: p.state === 'FAILED' ? 'error' : 'info',
        message: `${p.state.toLowerCase()} position ${short(p.mint)}${p.pnlSol === undefined ? '' : ` · ${formatSol(p.pnlSol)}`}`,
        entityMint: p.mint,
        payload: p,
      }),
    ),
    bus.on('exitTriggered', (e) =>
      record(repos, publisher, {
        category: 'exit',
        level: 'info',
        message: `Exit triggered for ${short(e.mint)}: ${e.trigger}${e.detail ? ` (${e.detail})` : ''}`,
        entityMint: e.mint,
        payload: e,
      }),
    ),
    bus.on('streamHealth', (e) =>
      record(repos, publisher, {
        category: 'stream_health',
        level: e.healthy ? 'info' : 'warn',
        message: `${e.source} stream ${e.healthy ? 'healthy' : 'down'}${e.detail ? `: ${e.detail}` : ''}`,
        payload: e,
      }),
    ),
    bus.on('breaker', (e) =>
      record(repos, publisher, {
        category: 'breaker',
        level: e.tripped ? 'error' : 'info',
        message: `${e.type} breaker ${e.tripped ? 'tripped' : 'cleared'}${e.detail ? `: ${e.detail}` : ''}`,
        payload: e,
      }),
    ),
    bus.on('killSwitch', (e) =>
      record(repos, publisher, {
        category: 'kill_switch',
        level: 'error',
        message: `Kill switch engaged by ${e.source}${e.detail ? `: ${e.detail}` : ''}`,
        payload: e,
      }),
    ),
    bus.on('alert', (a) =>
      record(repos, publisher, {
        category: 'notification',
        level: a.level,
        message: a.message,
        payload: a,
      }),
    ),
  ];

  return () => {
    for (const unsub of unsubs) unsub();
  };
}

function record(
  repos: Repositories,
  publisher: DashboardEventPublisher | undefined,
  input: OperatorEventInput,
): void {
  const id = repos.recordOperatorEvent(input);
  publisher?.publish({
    id,
    category: input.category,
    level: input.level,
    message: input.message,
    entityMint: input.entityMint ?? null,
    payload: input.payload ?? null,
    createdAt: new Date().toISOString(),
  });
}

function short(mint: string): string {
  return mint.length > 10 ? `${mint.slice(0, 4)}...${mint.slice(-4)}` : mint;
}

function formatSol(value: number): string {
  return `${value >= 0 ? '+' : ''}${value.toFixed(4)} SOL`;
}
