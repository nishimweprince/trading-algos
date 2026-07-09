/**
 * Active run session stamp for analytics attribution (config hash + id).
 * Set once at boot; read by guardrails/positions when persisting rows.
 */
export interface ActiveRunSession {
  id: number;
  configHash: string;
  mode: string;
}

let active: ActiveRunSession | null = null;

export function setActiveRunSession(session: ActiveRunSession | null): void {
  active = session;
}

export function getActiveRunSession(): ActiveRunSession | null {
  return active;
}
