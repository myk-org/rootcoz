/**
 * Full-page navigation to an absolute URL.
 *
 * Used where a client-side route change cannot do the job — following an IdP
 * logout URL, which has to be reached from the browser itself. One function so a
 * test can observe the destination without touching `window.location`, which
 * jsdom makes non-configurable.
 *
 * The scheme is re-checked here even though the server validates it: a server
 * bug must not become script execution in this origin.
 *
 * Returns whether the navigation was attempted, so a caller can fall back
 * instead of leaving the user on a page whose session was just cleared.
 */
export function hardNavigate(url: string): boolean {
  if (!/^https?:\/\//i.test(url)) return false
  window.location.assign(url)
  return true
}

/**
 * Rewrite the current history entry in place, keeping its state.
 *
 * The state is React Router's, and it holds `location.state.from` — the page to
 * return to. `replaceState(null, …)` would drop it, so a reload or a step back
 * after a URL cleanup lands on `/` instead of where the person came from.
 * Rewritten in place, so no extra history entry appears either.
 */
function rewriteUrl(search: string, hash: string): void {
  window.history.replaceState(
    window.history.state,
    '',
    `${window.location.pathname}${search}${hash}`,
  )
}

/**
 * Drop one-shot query parameters from the address bar, keeping the rest.
 *
 * An outcome reported in the query (`?sso_linked=…`, `?sso_error=…`) is read
 * once; left in the URL it shows a stale notice again on every refresh and
 * back-navigation.
 */
export function stripQueryParams(...keys: string[]): void {
  const params = new URLSearchParams(window.location.search)
  const before = params.toString()
  for (const key of keys) params.delete(key)
  const query = params.toString()
  if (query === before) return
  rewriteUrl(query ? `?${query}` : '', window.location.hash)
}

/**
 * Drop the fragment from the address bar, keeping the path and query.
 *
 * An unredeemed SSO ticket arrives in the fragment; it must not stay visible in
 * the address bar and history entry, nor be re-posted on the next reload.
 */
export function clearUrlFragment(): void {
  if (!window.location.hash) return
  rewriteUrl(window.location.search, '')
}
