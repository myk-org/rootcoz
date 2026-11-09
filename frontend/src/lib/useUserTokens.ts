import { useEffect, useState } from 'react'
import { api } from './api'

/** Whether the caller's account row holds a tracker credential — never the values. */
export interface UserTokenPresence {
  github: boolean
  jira: boolean
  loaded: boolean
}

/**
 * Presence of the caller's stored tracker credentials, read when `enabled` flips
 * true. A failed fetch reads as "no credential": the server is the authority, and
 * a false negative only hides the button while the server's own refusal names the
 * fix. Refetched every time `enabled` flips, so a token rotated in another browser
 * takes effect here without a reload.
 */
export function useUserTokenPresence(enabled: boolean): UserTokenPresence {
  const [presence, setPresence] = useState<UserTokenPresence>({
    github: false,
    jira: false,
    loaded: false,
  })
  useEffect(() => {
    if (!enabled) return
    let ignore = false
    api
      .get<{ github_token?: string; jira_token?: string; jira_sa_token?: string }>(
        '/api/user/tokens'
      )
      .then((tokens) => {
        if (ignore) return
        setPresence({
          github: !!tokens.github_token,
          jira: !!(tokens.jira_token || tokens.jira_sa_token),
          loaded: true,
        })
      })
      .catch(() => {
        if (ignore) return
        setPresence({ github: false, jira: false, loaded: true })
      })
    return () => {
      ignore = true
    }
  }, [enabled])
  return presence
}
