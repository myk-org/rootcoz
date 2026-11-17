import { expect, it } from 'vitest'

it('maps a known code and falls back for anything else', async () => {
  const { ssoErrorMessage } = await import('@/lib/ssoErrors')
  expect(ssoErrorMessage('identity_conflict', 'fallback')).toMatch(/already connected/)
  expect(ssoErrorMessage('nonsense', 'fallback')).toBe('fallback')
})

it('does not return an inherited property for a prototype key', async () => {
  const { ssoErrorMessage } = await import('@/lib/ssoErrors')
  // The code arrives from a query string: '__proto__' must fall back, not
  // return Object.prototype.
  expect(ssoErrorMessage('__proto__', 'fallback')).toBe('fallback')
  expect(ssoErrorMessage('constructor', 'fallback')).toBe('fallback')
})
