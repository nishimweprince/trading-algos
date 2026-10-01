import type { Config } from '../config/schema.ts';
import { ConfigError, readSecret } from '../config/load.ts';
import { JevHttpClient } from './jevClient.ts';
import { StubDecisionClient } from './stubClient.ts';
import type { DecisionClient } from './types.ts';

export type { Answer, DecisionClient, DecisionResult, Question } from './types.ts';
export { DecisionBreaker } from './breaker.ts';

/**
 * Build the configured decision provider, or null for `provider: none`.
 * `jev` without its API key is a startup error — a silently absent shadow
 * would look like "Jev had nothing to say".
 */
export function createDecisionClient(config: Config, opts: { fetchImpl?: typeof fetch } = {}): DecisionClient | null {
  const d = config.decision;
  if (d.provider === 'none') return null;
  if (d.provider === 'stub') return new StubDecisionClient();
  const apiKey = readSecret(d.jev.apiKeyEnvVar);
  if (!apiKey) throw new ConfigError(`decision.provider is jev but ${d.jev.apiKeyEnvVar} is not set`);
  return new JevHttpClient({
    url: d.jev.url,
    apiKey,
    model: d.jev.model,
    ...(opts.fetchImpl ? { fetchImpl: opts.fetchImpl } : {}),
  });
}
