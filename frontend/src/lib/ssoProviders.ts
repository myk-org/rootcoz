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

/**
 * Display names for the providers this UI knows, used where the server's own
 * `display_name` is not at hand — a linked identity carries only the provider key.
 *
 * It doubles as the enumeration the settings page splits `sso_<provider>_…` fields
 * by, which is why it lives here: one list, not one per component.
 */
export const SSO_PROVIDER_LABELS: Readonly<Record<string, string>> = Object.freeze({
  google: 'Google',
  github: 'GitHub',
  openshift: 'OpenShift',
  redhat: 'Red Hat SSO',
})

/** The display name for a provider key, or the key itself when unknown. */
export function ssoProviderLabel(provider: string): string {
  return SSO_PROVIDER_LABELS[provider] ?? provider
}

/**
 * The provider a settings key belongs to, or `null` for a generic SSO setting.
 *
 * Derived from the key — `sso_<provider>_<rest>` — rather than from a list of field
 * names, so a provider that gains a setting needs no change here. A key whose
 * provider token is not one we render stays generic, instead of disappearing into
 * a section nobody can see.
 */
export function ssoProviderOfKey(key: string): string | null {
  if (!key.startsWith('sso_')) return null
  const provider = key.slice('sso_'.length).split('_', 1)[0]
  return provider in SSO_PROVIDER_LABELS ? provider : null
}
