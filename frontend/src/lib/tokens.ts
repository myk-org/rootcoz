import { api, isExpectedTokenSyncError } from './api'

/** Persist tracker tokens to the server. Best-effort — errors are logged, not thrown.
 *
 * Sends all three fields every time: the server clears a field it receives as an
 * empty string and keeps one it never receives, so skipping an all-empty save
 * would make a cleared token impossible to remove (#294).
 */
export async function persistTokensToServer(gh: string, je: string, jt: string): Promise<void> {
  try {
    await api.put('/api/user/tokens', {
      github_token: gh,
      jira_email: je,
      jira_token: jt,
    })
  } catch (err) {
    if (!isExpectedTokenSyncError(err)) {
      console.error('Failed to sync tokens to server:', err)
    }
  }
}
