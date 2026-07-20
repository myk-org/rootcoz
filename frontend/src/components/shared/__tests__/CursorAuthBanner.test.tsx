import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { CursorAuthBanner } from '../CursorAuthBanner'

describe('CursorAuthBanner', () => {
  it('shows generic title when has_api_key is redacted (non-admin)', () => {
    render(
      <CursorAuthBanner
        status={{
          ok: false,
          reason: 'unavailable',
          hint: 'Cursor is unavailable. Contact an administrator.',
        }}
      />,
    )
    expect(screen.getByText(/Cursor unavailable \(unavailable\)/)).toBeDefined()
    expect(screen.queryByText(/browser login expired/i)).toBeNull()
    expect(screen.queryByText(/CURSOR_API_KEY/)).toBeNull()
  })

  it('shows login-expired diagnosis when admin has no API key', () => {
    render(
      <CursorAuthBanner
        status={{
          ok: false,
          reason: 'auth_expired',
          has_api_key: false,
        }}
      />,
    )
    expect(screen.getByText(/Cursor browser login expired/)).toBeDefined()
    expect(screen.getAllByText(/CURSOR_API_KEY/).length).toBeGreaterThan(0)
  })

  it('shows API-key-set diagnosis when admin has key configured', () => {
    render(
      <CursorAuthBanner
        status={{
          ok: false,
          reason: 'sidecar_error',
          has_api_key: true,
        }}
      />,
    )
    expect(screen.getByText(/API key is set/)).toBeDefined()
  })
})
