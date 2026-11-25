import { api } from '@/lib/api'

/** One SSO provider as `GET /api/auth/sso/providers` reports it. */
export interface SsoProvider {
  name: string
  display_name: string
}

/** One linked identity as `GET /api/user/identities` reports it. */
export interface LinkedIdentity {
  provider: string
  display_name?: string
  email: string | null
  created_at: string
}

/** Response body of `GET /api/user/identities`. */
export interface IdentitiesResponse {
  identities: LinkedIdentity[]
  unlinkable: boolean
  has_api_key?: boolean
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
 */
export function browserSignIn(data?: SsoProvidersResponse | null): BrowserSignIn {
  const configured = data?.providers ?? []
  if (data?.browser_login === false) {
    return { providers: [], cliOnly: configured.length > 0 }
  }
  return { providers: configured, cliOnly: false }
}

/**
 * Fetch and normalize browser-connectable SSO providers from the server.
 *
 * A deployment without SSO (or a transient probe failure) degrades to an empty
 * browser list without blocking the caller's UI.
 */
export async function fetchBrowserSignIn(): Promise<BrowserSignIn> {
  try {
    const offered = await api.get<SsoProvidersResponse>('/api/auth/sso/providers')
    return browserSignIn(offered)
  } catch {
    return { providers: [], cliOnly: false }
  }
}

/**
 * Whether an account may connect or disconnect SSO sign-in methods.
 *
 * Single source of truth shared by SignInMethodsCard and ConnectSignInMethodBanner:
 * viewers cannot perform account-security writes, and the bootstrap `admin` has no
 * users row to hold external identities.
 */
export function canManageSignInMethods(
  role: string | undefined,
  username: string | undefined,
  authenticated = true,
): boolean {
  return authenticated && role !== 'viewer' && username !== 'admin'
}

/**
 * Whether an account's linked SSO identities can currently be unlinked.
 *
 * Mirrors the backend rule in `auth/sso/flows.py`: an account with 2+ identities
 * or a local API key may unlink a provider; a keyless account's sole SSO identity
 * cannot be removed.
 */
export function isIdentityUnlinkable(
  identities: readonly unknown[],
  hasApiKey?: boolean,
): boolean {
  return identities.length > 1 || (identities.length === 1 && Boolean(hasApiKey))
}

/**
 * Resolve a provider's human-readable label strictly from server-supplied data.
 *
 * Checks an identity's server `display_name` first, then the configured provider
 * catalog from `/api/auth/sso/providers`, falling back to the raw provider name.
 */
export function resolveProviderDisplayName(
  provider: string,
  providers: readonly SsoProvider[] = [],
  identityDisplayName?: string,
): string {
  if (identityDisplayName) return identityDisplayName
  return providers.find((item) => item.name === provider)?.display_name ?? provider
}
