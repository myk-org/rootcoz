/**
 * One sentence per SSO refusal code — and the only copy of it.
 *
 * The authoritative list is `_SSO_PUBLIC_REASONS` (plus the `_SSO_DENIED`
 * catch-all) in `src/rootcoz/main.py`; add a line here when a reason is added
 * there. Two surfaces render these (the login page for a browser flow, the
 * Sign-in methods card for a link), which is exactly why the table lives in one
 * module: when it was copied, the two copies drifted within one stage.
 */
export const SSO_ERROR_MESSAGES: Record<string, string> = {
  unknown_provider: 'That sign-in provider is not configured on this server.',
  login_start_failed: 'The sign-in could not be started. Please try again.',
  state_invalid: 'Your sign-in attempt expired. Please try again.',
  state_mismatch: 'Your sign-in attempt expired. Please try again.',
  missing_code: 'The provider did not finish the sign-in. Please try again.',
  provider_error: 'The provider refused the sign-in. Please try again.',
  exchange_failed:
    'Could not complete the sign-in with the provider. Please try again.',
  id_token_invalid:
    'The provider returned an identity we could not verify. Please try again.',
  userinfo_failed:
    'Could not read your profile from the provider. Please try again.',
  no_subject: 'The provider returned no account identity. Please try again.',
  callback_misconfigured: 'SSO is misconfigured on this server. Contact an admin.',
  denied: "That account can't sign in with this provider.",
  domain_not_allowed:
    "This account's email domain is not allowed on this server. Contact an admin.",
  email_not_verified:
    'Your provider has not verified this email address, so it cannot be used here.',
  // Linking another provider (Profile → Sign-in methods).
  unknown_mode:
    'That sign-in link was not valid. Start again from Profile → Sign-in methods.',
  link_requires_session:
    'Your session ended before the provider answered. Sign in, then connect the provider again.',
  link_forbidden_role: 'A reviewer account is required to change sign-in methods.',
  link_session_changed:
    'The account changed while connecting. Sign in again and retry.',
  identity_conflict:
    'That provider account is already connected to a different user.',
}

/** The login page's generic sentence when the server sent no usable code. */
export const SSO_LOGIN_ERROR_FALLBACK =
  'Sign-in with the provider did not finish. Please try again.'

/** The card's generic sentence: the refusal happened while connecting. */
export const SSO_LINK_ERROR_FALLBACK =
  'That provider could not be connected. Please try again.'

/**
 * The sentence for a refusal code.
 *
 * @param code Reason code from `?sso_error=`, or empty.
 * @param fallback Sentence to use when the code is unknown or missing.
 * @returns The mapped sentence, or `fallback`.
 */
export function ssoErrorMessage(code: string, fallback: string): string {
  // `Object.hasOwn`, not a bare index: a plain object inherits `__proto__` and
  // friends, so `SSO_ERROR_MESSAGES['__proto__']` would return an object instead
  // of a sentence. The code arrives from a query string, so it can be anything.
  return Object.hasOwn(SSO_ERROR_MESSAGES, code)
    ? SSO_ERROR_MESSAGES[code]
    : fallback
}
