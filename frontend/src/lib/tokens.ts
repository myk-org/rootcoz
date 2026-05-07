import { api, isExpectedTokenSyncError } from './api'

/** Persist tracker tokens to the server. Best-effort — errors are logged, not thrown. */
export async function persistTokensToServer(gh: string, je: string, jt: string): Promise<void> {
  if (!gh && !je && !jt) return
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
