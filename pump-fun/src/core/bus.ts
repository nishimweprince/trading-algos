import { EventEmitter } from 'node:events';
import type {
  CandidateVerdict,
  ExitTrigger,
  GraduationEvent,
  Mint,
  PoolPricingRef,
  Position,
  VetoReason,
} from './types.ts';

/**
 * Typed in-process event bus (Section 3). A thin, strongly-typed wrapper over
 * EventEmitter with a discriminated-union event map. No microservices in v1;
 * a single process minimizes internal latency.
 *
 * Add new events here and every emit/on call is checked against the payload.
 */
export interface BusEventMap {
  /** A graduation was detected and de-duplicated across feeds. */
  graduation: GraduationEvent;
  /** Guardrail engine finished screening a candidate. */
  verdict: CandidateVerdict;
  /** An accepted candidate cleared for a position open (with pricing refs). */
  openPosition: { mint: Mint; sizeSol: number; highVolatility: boolean; pricing: PoolPricingRef };
  /** An entry was blocked before capital was committed. */
  entryVetoed: { mint: Mint; reason: VetoReason; detail?: string };
  /** Position FSM transitioned. */
  positionUpdate: Position;
  /** An exit trigger fired for an open position. */
  exitTriggered: { mint: Mint; trigger: ExitTrigger; detail?: string };
  /** Detection feed health changed (Section 8 stream-health breaker). */
  streamHealth: { source: string; healthy: boolean; detail?: string };
  /** Risk manager tripped or cleared a circuit breaker. */
  breaker: { type: string; tripped: boolean; detail?: string };
  /** Global kill switch engaged. */
  killSwitch: { source: 'telegram' | 'file' | 'internal'; detail?: string };
  /** Anything worth surfacing to the operator via Telegram. */
  alert: { level: 'info' | 'warn' | 'error'; message: string };
}

export type BusEvent = keyof BusEventMap;

export class TypedBus {
  private readonly emitter = new EventEmitter();

  constructor() {
    // These are trading events, not leaks — allow many subscribers.
    this.emitter.setMaxListeners(100);
  }

  emit<E extends BusEvent>(event: E, payload: BusEventMap[E]): void {
    this.emitter.emit(event, payload);
  }

  on<E extends BusEvent>(event: E, handler: (payload: BusEventMap[E]) => void): () => void {
    this.emitter.on(event, handler as (...args: unknown[]) => void);
    return () => this.emitter.off(event, handler as (...args: unknown[]) => void);
  }

  once<E extends BusEvent>(event: E, handler: (payload: BusEventMap[E]) => void): void {
    this.emitter.once(event, handler as (...args: unknown[]) => void);
  }

  off<E extends BusEvent>(event: E, handler: (payload: BusEventMap[E]) => void): void {
    this.emitter.off(event, handler as (...args: unknown[]) => void);
  }

  removeAll(): void {
    this.emitter.removeAllListeners();
  }
}
