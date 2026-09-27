import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { UsersPage } from '@/pages/UsersPage'

const get = vi.fn()
const post = vi.fn()
const put = vi.fn()
const refreshAuth = vi.fn()
vi.mock('@/lib/api', () => ({ api: { get: (...args: unknown[]) => get(...args), post: (...args: unknown[]) => post(...args), put: (...args: unknown[]) => put(...args) }, ApiError: class extends Error {} }))
vi.mock('@/lib/auth', () => ({ useAuth: () => ({ username: 'admin2', refreshAuth }) }))

const users = [
  { username: 'alice', role: 'reviewer', status: 'active', can_use_server_providers: false, can_view_reports: false, created_at: '', last_seen: null },
  { username: 'bob', role: 'reviewer', status: 'pending', can_use_server_providers: false, can_view_reports: false, created_at: '', last_seen: null },
]

afterEach(() => { get.mockReset(); post.mockReset(); put.mockReset(); refreshAuth.mockReset() })

describe('server provider grants', () => {
  it('sends explicit grants on create and approval, then revokes via row switch', async () => {
    get.mockResolvedValue({ users })
    post.mockResolvedValue({ username: 'new', api_key: '' })
    put.mockResolvedValue({})
    const user = userEvent.setup()
    render(<UsersPage />)
    await user.click(await screen.findByRole('button', { name: 'Add User' }))
    await user.type(screen.getByPlaceholderText('e.g. john.doe'), 'new')
    await user.click(screen.getByRole('switch', { name: 'Allow server providers' }))
    await user.click(screen.getByRole('button', { name: 'Create' }))
    await waitFor(() => expect(post).toHaveBeenCalledWith('/api/admin/users/create', expect.objectContaining({ can_use_server_providers: true })))
    await user.click(screen.getByRole('button', { name: 'Done' }))
    await user.click(screen.getByRole('button', { name: 'Approve bob' }))
    expect(post).not.toHaveBeenCalledWith('/api/admin/users/bob/approve', expect.anything())
    await user.click(screen.getByRole('switch', { name: 'Allow server providers on approval' }))
    await user.click(screen.getByRole('button', { name: 'Approve user' }))
    await waitFor(() => expect(post).toHaveBeenCalledWith('/api/admin/users/bob/approve', { can_use_server_providers: true }))
    await user.click(screen.getByRole('switch', { name: 'Allow server providers for alice' }))
    await waitFor(() => expect(put).toHaveBeenCalledWith('/api/admin/users/alice/can-use-server-providers', { can_use_server_providers: true }))
    await user.click(screen.getByRole('switch', { name: 'Allow server providers for alice' }))
    await waitFor(() => expect(put).toHaveBeenCalledWith('/api/admin/users/alice/can-use-server-providers', { can_use_server_providers: false }))
  })
})
