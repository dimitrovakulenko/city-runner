import type { BrowserSessions } from './session';

/** Only the explicitly enabled loopback development client can request a fixture session. */
export async function restoreDevelopmentSession(sessions: BrowserSessions, enabled: boolean, hostname: string, fetchImpl = globalThis.fetch): Promise<void> {
  if (!enabled || !['127.0.0.1', 'localhost', '[::1]'].includes(hostname) || await sessions.getToken()) return;
  const response = await fetchImpl('/api/dev/session', { method: 'POST' });
  if (!response.ok) return;
  const { session_token } = await response.json();
  if (typeof session_token === 'string' && session_token) await sessions.setTokenIfEmpty(session_token);
}
