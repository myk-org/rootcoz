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
