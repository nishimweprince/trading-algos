/**
 * Consecutive-failure breaker for the decision provider (same cooldown idea as
 * core/rpc.ts's per-endpoint cooldown). After `failures` misses in a row the
 * provider is skipped for `cooldownMs`; the next call after that is a probe.
 * Also keeps the running counters the ops view reports.
 */
export interface DecisionBreakerStats {
  calls: number;
  failures: number;
  skipped: number;
  inputTokens: number;
  open: boolean;
}

export class DecisionBreaker {
  private consecutive = 0;
  private openUntilMs = 0;
  private readonly stats = { calls: 0, failures: 0, skipped: 0, inputTokens: 0 };

  private readonly cfg: { failures: number; cooldownMs: number };
  private readonly now: () => number;

  constructor(cfg: { failures: number; cooldownMs: number }, now: () => number = Date.now) {
    this.cfg = cfg;
    this.now = now;
  }

  /** True when a call may go out; counts a skip otherwise. */
  allow(): boolean {
    if (this.now() < this.openUntilMs) {
      this.stats.skipped++;
      return false;
    }
    return true;
  }

  success(inputTokens: number): void {
    this.stats.calls++;
    this.stats.inputTokens += inputTokens;
    this.consecutive = 0;
  }

  failure(): void {
    this.stats.calls++;
    this.stats.failures++;
    this.consecutive++;
    if (this.consecutive >= this.cfg.failures) {
      this.openUntilMs = this.now() + this.cfg.cooldownMs;
      this.consecutive = 0;
    }
  }

  snapshot(): DecisionBreakerStats {
    return { ...this.stats, open: this.now() < this.openUntilMs };
  }
}
