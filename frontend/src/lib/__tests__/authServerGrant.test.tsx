import { render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { AuthProvider, useAuth } from '@/lib/auth'

const get = vi.fn()
vi.mock('@/lib/api', async (importActual) => ({
  ...await importActual<typeof import('@/lib/api')>(),
  api: { get: (...args: unknown[]) => get(...args) },
}))

function Grant() {
  const { canUseServerProviders } = useAuth()
  return <span>{canUseServerProviders ? 'Allowed' : 'Denied'}</span>
}

afterEach(() => get.mockReset())

it('treats admins as granted despite a false stored flag, but not ungranted non-admins', async () => {
  get.mockImplementation(async (path: string) => path === '/api/auth/me'
    ? { username: 'admin2', role: 'admin', is_admin: true, can_view_reports: true, can_use_server_providers: false }
    : { github_token: '', jira_email: '', jira_token: '' })
  const { unmount } = render(<AuthProvider><Grant /></AuthProvider>)
  await waitFor(() => expect(screen.getByText('Allowed')).toBeInTheDocument())
  unmount()
  get.mockImplementation(async (path: string) => path === '/api/auth/me'
    ? { username: 'alice', role: 'reviewer', is_admin: false, can_view_reports: false, can_use_server_providers: false }
    : { github_token: '', jira_email: '', jira_token: '' })
  render(<AuthProvider><Grant /></AuthProvider>)
  await waitFor(() => expect(get).toHaveBeenCalledWith('/api/user/tokens'))
  expect(screen.getByText('Denied')).toBeInTheDocument()
})
