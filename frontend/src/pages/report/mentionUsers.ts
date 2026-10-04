import { api } from '@/lib/api'

/* ------------------------------------------------------------------ */
/*  Module-level cache for mentionable users                           */
/* ------------------------------------------------------------------ */

export let cachedUsers: string[] | null = null
let fetchPromise: Promise<string[]> | null = null

export async function fetchMentionableUsers(): Promise<string[]> {
  if (cachedUsers) return cachedUsers
  if (fetchPromise) return fetchPromise
  fetchPromise = api
    .get<{ usernames: string[] }>('/api/users/mentionable')
    .then((res) => {
      cachedUsers = res.usernames ?? []
      return cachedUsers
    })
    .catch(() => {
      fetchPromise = null
      return []
    })
  return fetchPromise
}

/** Exported for testing only — resets the module-level cache. */
export function _resetMentionCache() {
  cachedUsers = null
  fetchPromise = null
}
