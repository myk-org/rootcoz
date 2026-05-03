const COOKIE_NAME = 'jji_username'
const GITHUB_TOKEN_KEY = 'jji_github_token'
const JIRA_TOKEN_KEY = 'jji_jira_token'
const JIRA_EMAIL_KEY = 'jji_jira_email'
// Display-only UI hints — NOT an authorization boundary.
// All admin gating is enforced server-side in AuthMiddleware.
const ADMIN_KEY = 'jji_is_admin'
const ROLE_KEY = 'jji_role'

/** Detects values that look like they've been URL-encoded (e.g. %40, %25). */
const ENCODED_PATTERN = /%[0-9A-Fa-f]{2}/

/**
 * RFC 6265 cookie-octet invalid characters:
 * CTL (0x00-0x1F, 0x7F), SP (0x20), DQUOTE (0x22), comma (0x2C),
 * semicolon (0x3B), backslash (0x5C).
 *
 * Note: @ (0x40) and + (0x2B) are valid cookie-octet characters,
 * so email-style usernames like user@example.com need no encoding.
 */
const RFC6265_INVALID = /[\x00-\x1f\x7f\x20",;\\]/g

export function looksUrlEncoded(value: string): boolean {
  return ENCODED_PATTERN.test(value)
}

export function getUsername(): string {
  const match = document.cookie.match(
    new RegExp(`(?:^|;\\s*)${COOKIE_NAME}=([^;]*)`)
  )
  if (!match) return ''
  try {
    return decodeURIComponent(match[1])
  } catch {
    return match[1]
  }
}

export function setUsername(username: string): void {
  const trimmed = username.trim()
  if (looksUrlEncoded(trimmed)) {
    throw new Error(`Username "${trimmed}" looks URL-encoded. Use the raw value.`)
  }
  // Percent-encode only RFC 6265 invalid characters for safe cookie storage
  const safe = trimmed.replace(RFC6265_INVALID, (ch) =>
    `%${ch.charCodeAt(0).toString(16).padStart(2, '0').toUpperCase()}`
  )
  document.cookie = `${COOKIE_NAME}=${safe}; path=/; max-age=${365 * 24 * 60 * 60}; SameSite=Lax`
}

export function clearUsername(): void {
  document.cookie = `${COOKIE_NAME}=; path=/; max-age=0; SameSite=Lax`
}

export function isLoggedIn(): boolean {
  return getUsername() !== ''
}

function readStoredValue(key: string): string {
  try {
    return localStorage.getItem(key) ?? ''
  } catch {
    return ''
  }
}

function writeStoredValue(key: string, value: string): void {
  try {
    const trimmed = value.trim()
    if (trimmed) {
      localStorage.setItem(key, trimmed)
    } else {
      localStorage.removeItem(key)
    }
  } catch {
    // Storage unavailable — silently ignore
  }
}

export function getGithubToken(): string {
  return readStoredValue(GITHUB_TOKEN_KEY)
}

export function setGithubToken(token: string): void {
  writeStoredValue(GITHUB_TOKEN_KEY, token)
}

export function getJiraToken(): string {
  return readStoredValue(JIRA_TOKEN_KEY)
}

export function setJiraToken(token: string): void {
  writeStoredValue(JIRA_TOKEN_KEY, token)
}

export function getJiraEmail(): string {
  return readStoredValue(JIRA_EMAIL_KEY)
}

export function setJiraEmail(email: string): void {
  writeStoredValue(JIRA_EMAIL_KEY, email)
}

export function getIsAdmin(): boolean {
  try {
    return localStorage.getItem(ADMIN_KEY) === 'true'
  } catch {
    return false
  }
}

export function setIsAdmin(isAdmin: boolean): void {
  try {
    if (isAdmin) {
      localStorage.setItem(ADMIN_KEY, 'true')
    } else {
      localStorage.removeItem(ADMIN_KEY)
    }
  } catch {
    // ignore
  }
}

export function getRole(): string {
  try {
    return localStorage.getItem(ROLE_KEY) ?? 'user'
  } catch {
    return 'user'
  }
}

export function setRole(role: string): void {
  try {
    if (role && role !== 'user') {
      localStorage.setItem(ROLE_KEY, role)
    } else {
      localStorage.removeItem(ROLE_KEY)
    }
  } catch {
    // ignore
  }
}

export function clearTokens(): void {
  try {
    localStorage.removeItem(GITHUB_TOKEN_KEY)
    localStorage.removeItem(JIRA_TOKEN_KEY)
    localStorage.removeItem(JIRA_EMAIL_KEY)
    localStorage.removeItem(ADMIN_KEY)
    localStorage.removeItem(ROLE_KEY)
  } catch {
    // Storage unavailable — silently ignore
  }
}
