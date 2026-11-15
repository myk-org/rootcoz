/** One SSO provider as `GET /api/auth/sso/providers` reports it. */
export interface SsoProvider {
  name: string
  display_name: string
}

/**
 * The body of `GET /api/auth/sso/providers`.
 *
 * `browser_login` is false when the deployment has no public origin
 * (`PUBLIC_BASE_URL` unset): a provider callback URL cannot be built, so the
 * login start refuses with 400 *after* the click. Only the CLI device flow
 * (`rootcoz auth sso`) works there, and it joins a relative verification URL
 * itself.
 */
export interface SsoProvidersResponse {
  providers?: SsoProvider[]
  browser_login?: boolean
}

export interface BrowserSignIn {
  /** The providers a browser flow can actually connect. */
  providers: SsoProvider[]
  /** Providers exist, but only the CLI device flow can reach them. */
  cliOnly: boolean
}

/**
 * Split the endpoint's answer into what a browser may offer and why it may not.
 *
 * The flag is authoritative and this is the only copy of the rule — the login
 * page, the dashboard prompt and the Sign-in methods card all read it here, so a
 * browser-flow control is never rendered on a deployment that would refuse it.
 *
 * @param data The endpoint's body, or nothing when the request failed.
 * @returns The connectable providers, and whether the server is CLI-only.
 */
export function browserSignIn(data?: SsoProvidersResponse | null): BrowserSignIn {
  const configured = data?.providers ?? []
  if (data?.browser_login === false) {
    return { providers: [], cliOnly: configured.length > 0 }
  }
  return { providers: configured, cliOnly: false }
}
