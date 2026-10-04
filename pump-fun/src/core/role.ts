export type ProcessRole = 'all' | 'trader' | 'research';

/** PUMPDESK_ROLE (pm2 env) overrides runtime.role from the config file. */
export function resolveRole(env: string | undefined, configured: ProcessRole): ProcessRole {
  if (env === undefined || env === '') return configured;
  if (env === 'all' || env === 'trader' || env === 'research') return env;
  throw new Error(`PUMPDESK_ROLE must be all | trader | research (got ${env})`);
}
