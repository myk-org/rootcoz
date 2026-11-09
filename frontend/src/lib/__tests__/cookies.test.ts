import { describe, it, expect, beforeEach, vi } from 'vitest'
import { getUsername, setUsername, isLoggedIn, clearTokens, looksUrlEncoded, getIsAdmin, getRole } from '../cookies'

describe('cookies', () => {
  beforeEach(() => {
    // Clear all cookies
    document.cookie.split(';').forEach((c) => {
      const name = c.split('=')[0].trim()
      if (name) document.cookie = `${name}=; max-age=0; path=/`
    })
    // Clear localStorage
    localStorage.clear()
  })

  it('getUsername returns empty when no cookie set', () => {
    expect(getUsername()).toBe('')
  })

  it('setUsername sets cookie and getUsername reads it', () => {
    setUsername('testuser')
    expect(getUsername()).toBe('testuser')
  })

  it('setUsername trims whitespace', () => {
    setUsername('  padded  ')
    expect(getUsername()).toBe('padded')
  })

  it('handles usernames with special characters without double-encoding', () => {
    setUsername('user@example.com')
    expect(getUsername()).toBe('user@example.com')
    // Writing the same value again should not mutate it
    const readBack = getUsername()
    setUsername(readBack)
    expect(getUsername()).toBe('user@example.com')
  })

  it('handles usernames with + character', () => {
    setUsername('user+tag@example.com')
    expect(getUsername()).toBe('user+tag@example.com')
    // No double-encoding on re-read/re-write
    setUsername(getUsername())
    expect(getUsername()).toBe('user+tag@example.com')
  })

  it('handles usernames with spaces', () => {
    setUsername('John Doe')
    expect(getUsername()).toBe('John Doe')
    setUsername(getUsername())
    expect(getUsername()).toBe('John Doe')
  })

  it('rejects already-encoded usernames', () => {
    expect(() => setUsername('user%40example.com')).toThrow(/looks URL-encoded/)
    expect(() => setUsername('user%2540example.com')).toThrow(/looks URL-encoded/)
    expect(() => setUsername('hello%25world')).toThrow(/looks URL-encoded/)
  })

  describe('looksUrlEncoded', () => {
    it('returns true for percent-encoded strings', () => {
      expect(looksUrlEncoded('%40')).toBe(true)
      expect(looksUrlEncoded('user%40domain')).toBe(true)
      expect(looksUrlEncoded('%25')).toBe(true)
      expect(looksUrlEncoded('%2540')).toBe(true)
    })

    it('returns false for normal strings', () => {
      expect(looksUrlEncoded('hello')).toBe(false)
      expect(looksUrlEncoded('user@domain.com')).toBe(false)
      expect(looksUrlEncoded('')).toBe(false)
    })
  })

  it('isLoggedIn returns false when no cookie', () => {
    expect(isLoggedIn()).toBe(false)
  })

  it('isLoggedIn returns true after setUsername', () => {
    setUsername('user1')
    expect(isLoggedIn()).toBe(true)
  })

  // --- No tracker token survives in web storage ---

  const TRACKER_KEYS = [
    'rootcoz_github_token',
    'rootcoz_jira_token',
    'rootcoz_jira_email',
    'rootcoz_jira_sa_token',
    'jji_github_token',
    'jji_jira_token',
    'jji_jira_email',
  ]

  it('test_load_scrubs_every_stored_tracker_token', async () => {
    // A browser that upgraded still carries the copy, current and legacy keys.
    for (const key of TRACKER_KEYS) localStorage.setItem(key, 'secret')
    localStorage.setItem('jji_is_admin', 'true')
    localStorage.setItem('jji_role', 'admin')

    // Module load runs the migration + scrub.
    vi.resetModules()
    await import('../cookies')

    for (const key of TRACKER_KEYS) expect(localStorage.getItem(key)).toBeNull()
    // The display hints are still migrated
    expect(getIsAdmin()).toBe(true)
    expect(getRole()).toBe('admin')
  })

  it('clearTokens also removes stored tracker tokens', () => {
    // A sign-out on a browser that still holds a legacy key must drop it too.
    for (const key of TRACKER_KEYS) localStorage.setItem(key, 'secret')
    localStorage.setItem('rootcoz_is_admin', 'true')

    clearTokens()

    for (const key of TRACKER_KEYS) expect(localStorage.getItem(key)).toBeNull()
    expect(localStorage.getItem('rootcoz_is_admin')).toBeNull()
  })
})
