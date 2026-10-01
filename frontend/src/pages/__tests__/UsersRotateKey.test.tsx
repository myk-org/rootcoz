import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { UsersPage } from '@/pages/UsersPage'

const { get, post, ApiError } = vi.hoisted(() => {
  class ApiError extends Error {
    status: number
    body: unknown
    constructor(status: number, body: unknown) {
      super('api error')
      this.status = status
      this.body = body
    }
  }
  return { get: vi.fn(), post: vi.fn(), ApiError }
})

vi.mock('@/lib/api', () => ({ api: { get, post, put: vi.fn(), delete: vi.fn() }, ApiError, extractApiDetail: () => null }))
vi.mock('@/lib/auth', () => ({ useAuth: () => ({ username: 'admin2', refreshAuth: vi.fn() }) }))

const users = [
  { username: 'alice', role: 'reviewer', status: 'active', can_use_server_providers: false, can_view_reports: false, created_at: '', last_seen: null },
  { username: 'mallory', role: 'reviewer', status: 'rejected', can_use_server_providers: false, can_view_reports: false, created_at: '', last_seen: null },
  { username: 'admin2', role: 'admin', status: 'active', can_use_server_providers: false, can_view_reports: false, created_at: '', last_seen: null },
]

HTMLElement.prototype.hasPointerCapture = () => false
HTMLElement.prototype.setPointerCapture = () => {}
HTMLElement.prototype.releasePointerCapture = () => {}
HTMLElement.prototype.scrollIntoView = () => {}

afterEach(() => {
  get.mockReset()
  post.mockReset()
})

describe('admin API key rotation', () => {
  it('confirms first, then shows the new key once with a copy button', async () => {
    get.mockResolvedValue({ users })
    post.mockResolvedValue({ username: 'alice', new_api_key: 'rk_new_secret' }) // pragma: allowlist secret
    const user = userEvent.setup()
    render(<UsersPage />)

    await user.click(await screen.findByRole('button', { name: 'Rotate key for alice' }))

    // Confirmation step — nothing is sent until the admin confirms.
    const confirmDialog = await screen.findByRole('dialog')
    expect(within(confirmDialog).getByText(/invalidated immediately/i)).toBeInTheDocument()
    expect(post).not.toHaveBeenCalled()

    await user.click(within(confirmDialog).getByRole('button', { name: 'Rotate Key' }))

    await waitFor(() =>
      expect(post).toHaveBeenCalledWith('/api/admin/users/alice/rotate-key'),
    )
    // The key is shown exactly once, with a copy button and a save-now warning.
    const resultDialog = await screen.findByRole('dialog')
    expect(within(resultDialog).getByText('rk_new_secret')).toBeInTheDocument()
    expect(within(resultDialog).getByRole('button', { name: 'Copy to clipboard' })).toBeInTheDocument()
    expect(within(resultDialog).getByText(/cannot be retrieved later/i)).toBeInTheDocument()

    // Closing discards the key for good.
    await user.click(within(resultDialog).getByRole('button', { name: 'Done' }))
    expect(screen.queryByText('rk_new_secret')).not.toBeInTheDocument()
  })

  it('offers no rotate action for a rejected user', async () => {
    get.mockResolvedValue({ users })
    render(<UsersPage />)

    expect(await screen.findByText('mallory')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Rotate key for mallory' })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Rotate key for alice' })).toBeInTheDocument()
    expect(post).not.toHaveBeenCalled()
  })

  it('keeps the confirmation open and shows the error when rotation fails', async () => {
    get.mockResolvedValue({ users })
    post.mockRejectedValue(new ApiError(404, { detail: 'User not found' }))
    const user = userEvent.setup()
    render(<UsersPage />)

    await user.click(await screen.findByRole('button', { name: 'Rotate key for alice' }))
    const confirmDialog = await screen.findByRole('dialog')
    await user.click(within(confirmDialog).getByRole('button', { name: 'Rotate Key' }))

    await waitFor(() =>
      expect(within(screen.getByRole('dialog')).getByRole('alert')).toHaveTextContent('User not found'),
    )
    expect(screen.queryByText(/New API Key/)).not.toBeInTheDocument()
  })
})
