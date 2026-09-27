import { render, screen, waitFor, within } from '@testing-library/react'
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
  { username: 'legacy', role: 'viewer', status: 'pending', can_use_server_providers: true, can_view_reports: false, created_at: '', last_seen: null },
  { username: 'admin2', role: 'admin', status: 'active', can_use_server_providers: false, can_view_reports: false, created_at: '', last_seen: null },
]

HTMLElement.prototype.hasPointerCapture = () => false
HTMLElement.prototype.setPointerCapture = () => {}
HTMLElement.prototype.releasePointerCapture = () => {}
HTMLElement.prototype.scrollIntoView = () => {}

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
    expect(screen.getByRole('switch', { name: 'Allow server providers' })).toHaveAttribute('aria-checked', 'false')
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

  it('shows migrated grants, but admins always have access even when the stored flag is false', async () => {
    get.mockResolvedValue({ users })
    post.mockResolvedValue({})
    const user = userEvent.setup()
    render(<UsersPage />)
    const adminRow = (await screen.findByText('admin2')).closest('tr')!
    expect(within(adminRow).getAllByText('Always')).toHaveLength(2)
    expect(within(adminRow).queryByRole('switch', { name: 'Allow server providers for admin2' })).not.toBeInTheDocument()
    expect(screen.getByRole('switch', { name: 'Allow server providers for alice' })).toHaveAttribute('aria-checked', 'false')
    expect(screen.getByRole('switch', { name: 'Allow server providers for legacy' })).toBeDisabled()
    expect(screen.getByRole('switch', { name: 'Allow server providers for legacy' })).toHaveAttribute('aria-checked', 'true')
    await user.click(screen.getByRole('button', { name: 'Approve legacy' }))
    expect(screen.getByRole('switch', { name: 'Allow server providers on approval' })).toHaveAttribute('aria-checked', 'true')
    await user.click(screen.getByRole('button', { name: 'Approve user' }))
    await waitFor(() => expect(post).toHaveBeenCalledWith('/api/admin/users/legacy/approve', { can_use_server_providers: true }))
    expect(put).not.toHaveBeenCalledWith('/api/admin/users/admin2/can-use-server-providers', expect.anything())
  })

  it('does not persist an implicit grant when a new admin is demoted', async () => {
    let boss: (typeof users)[number] | undefined
    get.mockImplementation(async () => ({ users: boss ? [...users, boss] : users }))
    post.mockImplementation(async (_path, payload) => {
      boss = { username: payload.username, role: payload.role, status: 'active', can_use_server_providers: payload.can_use_server_providers, can_view_reports: false, created_at: '', last_seen: null }
      return { username: boss.username, api_key: 'secret' } // pragma: allowlist secret
    })
    put.mockImplementation(async (_path, payload) => {
      boss = { ...boss!, role: payload.role }
      return { username: boss.username, role: boss.role }
    })
    const user = userEvent.setup()
    render(<UsersPage />)
    await user.click(await screen.findByRole('button', { name: 'Add User' }))
    await user.type(screen.getByPlaceholderText('e.g. john.doe'), 'boss')
    await user.click(screen.getByRole('switch', { name: 'Allow server providers' }))
    await user.click(within(screen.getByRole('dialog')).getByRole('combobox'))
    await user.click(screen.getByRole('option', { name: 'Admin' }))
    expect(screen.queryByRole('switch', { name: 'Allow server providers' })).not.toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Create' }))
    await waitFor(() => expect(post).toHaveBeenCalledWith('/api/admin/users/create', expect.objectContaining({ role: 'admin', can_use_server_providers: false })))
    await user.click(screen.getByRole('button', { name: 'Done' }))
    const bossRow = (await screen.findByText('boss')).closest('tr')!
    expect(within(bossRow).getAllByText('Always')).toHaveLength(2)
    await user.click(within(bossRow).getByRole('combobox'))
    await user.click(screen.getByRole('option', { name: 'reviewer' }))
    await user.click(screen.getByRole('button', { name: 'Change Role' }))
    await user.click(screen.getByRole('button', { name: 'Done' }))
    await waitFor(() => expect(within(bossRow).getByRole('switch', { name: 'Allow server providers for boss' })).toHaveAttribute('aria-checked', 'false'))
    await user.click(screen.getByRole('button', { name: 'Add User' }))
    await user.click(within(screen.getByRole('dialog')).getByRole('combobox'))
    await user.click(screen.getByRole('option', { name: 'Admin' }))
    await user.click(within(screen.getByRole('dialog')).getByRole('combobox'))
    await user.click(screen.getByRole('option', { name: 'Reviewer' }))
    expect(screen.getByRole('switch', { name: 'Allow server providers' })).toHaveAttribute('aria-checked', 'false')
  })
})
