import { describe, expect, it } from 'vitest'
import {
  browserSignIn,
  canManageSignInMethods,
  isIdentityUnlinkable,
  resolveProviderDisplayName,
} from '@/lib/ssoProviders'

describe('ssoProviders helpers', () => {
  it('evaluates sign-in method management eligibility once', () => {
    expect(canManageSignInMethods('reviewer', 'alice')).toBe(true)
    expect(canManageSignInMethods('operator', 'alice')).toBe(true)
    expect(canManageSignInMethods('admin', 'boss')).toBe(true)
    expect(canManageSignInMethods('viewer', 'vicky')).toBe(false)
    expect(canManageSignInMethods('admin', 'admin')).toBe(false)
    expect(canManageSignInMethods('reviewer', 'alice', false)).toBe(false)
  })

  it('evaluates identity unlinkability matching backend rules', () => {
    expect(isIdentityUnlinkable([], false)).toBe(false)
    expect(isIdentityUnlinkable([], true)).toBe(false)
    expect(isIdentityUnlinkable(['redhat'], false)).toBe(false)
    expect(isIdentityUnlinkable(['redhat'], true)).toBe(true)
    expect(isIdentityUnlinkable(['redhat', 'github'], false)).toBe(true)
  })

  it('resolves display names from server data without hardcoded maps', () => {
    const catalog = [{ name: 'redhat', display_name: 'Red Hat SSO' }]
    expect(resolveProviderDisplayName('redhat', catalog, 'Custom Label')).toBe('Custom Label')
    expect(resolveProviderDisplayName('redhat', catalog)).toBe('Red Hat SSO')
    expect(resolveProviderDisplayName('unknown-idp', catalog)).toBe('unknown-idp')
  })

  it('splits browser_login=false into cliOnly', () => {
    const result = browserSignIn({
      providers: [{ name: 'redhat', display_name: 'Red Hat SSO' }],
      browser_login: false,
    })
    expect(result).toEqual({ providers: [], cliOnly: true })
  })
})
