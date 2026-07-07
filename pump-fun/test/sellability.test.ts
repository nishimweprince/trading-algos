import { describe, it, expect } from 'vitest';
import { checkSellability } from '../src/guardrails/checks/pending.ts';
import type { CheckContext } from '../src/guardrails/engine.ts';
import type { Candidate } from '../src/enrichment/types.ts';

function ctx(sellable?: Candidate['enrichment']['sellable']): CheckContext {
  return {
    candidate: {
      graduation: { mint: 'M', venue: 'pumpswap', poolAddress: '', slot: 1, feedSource: 'pumpportal', receivedAtNs: 0n },
      enrichment: { unknowns: [], elapsedMs: 1, ...(sellable ? { sellable } : {}) },
    },
    // config/repos/mode unused by checkSellability
    config: {} as CheckContext['config'],
    repos: {} as CheckContext['repos'],
    mode: 'live',
  };
}

describe('H4 checkSellability', () => {
  it('is unknown when the probe did not run', () => {
    expect(checkSellability(ctx()).status).toBe('unknown');
  });
  it('passes when the atomic buy+sell simulated cleanly', () => {
    expect(checkSellability(ctx({ status: 'pass', detail: 'ok' })).status).toBe('pass');
  });
  it('fails (honeypot) when the sell leg was rejected', () => {
    const r = checkSellability(ctx({ status: 'fail', detail: 'sell leg failed' }));
    expect(r.status).toBe('fail');
    expect(r.detail).toContain('sell leg');
  });
  it('stays unknown on an inconclusive probe (e.g. unfunded wallet)', () => {
    expect(checkSellability(ctx({ status: 'unknown', detail: 'inconclusive' })).status).toBe('unknown');
  });
});
