import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { RegisterPage } from '@/pages/RegisterPage'

const apiMock = vi.hoisted(() => ({
  get: vi.fn(async () => ({})),
  post: vi.fn(async (path: string) => {
    if (path === '/api/auth/register') {
      return { api_key: 'key', username: 'jdoe', status: 'active' } // pragma: allowlist secret
    }
    return {}
  }),
  put: vi.fn(async () => ({})),
}))
vi.mock('@/lib/api', () => ({
  api: apiMock,
  ApiError: class ApiError extends Error {
    status: number
    body: unknown
    constructor(status: number, body: unknown) {
      super('api error')
      this.status = status
      this.body = body
    }
  },
  isExpectedTokenSyncError: () => false,
}))

vi.mock('@/lib/auth', () => ({
  useAuth: () => ({
    login: vi.fn(),
    refreshAuth: vi.fn(),
    authenticated: false,
    loading: false,
    username: null,
  }),
}))

function renderRegister() {
  return render(
    <MemoryRouter>
      <RegisterPage />
    </MemoryRouter>,
  )
}

/** The page opens on the login form; click through to the register form. */
async function openRegisterForm() {
  await userEvent.click(await screen.findByRole('button', { name: /^register$/i }))
  return screen.findByLabelText(/username/i)
}

describe('RegisterPage token persistence', () => {
  beforeEach(() => {
    localStorage.clear()
    apiMock.get.mockClear()
    apiMock.post.mockClear()
    apiMock.put.mockClear()
  })

  it('does not send an empty token save for a keyless account (#294)', async () => {
    renderRegister()
    const username = await openRegisterForm()
    await userEvent.type(username, 'jdoe')
    await userEvent.click(screen.getByRole('button', { name: /^register$/i }))

    await waitFor(() => expect(apiMock.post).toHaveBeenCalledWith('/api/auth/register', expect.anything()))
    // An empty field is a clear server-side, so an untouched registration form
    // must not PUT — the account may already hold stored credentials.
    expect(apiMock.put).not.toHaveBeenCalledWith('/api/user/tokens', expect.anything())
  })

  it('sends the tokens a reviewer did type', async () => {
    renderRegister()
    const username = await openRegisterForm()
    await userEvent.type(username, 'jdoe')
    const github = screen.getByLabelText(/github token/i)
    await userEvent.type(github, 'typed_token')
    await userEvent.click(screen.getByRole('button', { name: /^register$/i }))

    await waitFor(() =>
      expect(apiMock.put).toHaveBeenCalledWith('/api/user/tokens', expect.objectContaining({ github_token: 'typed_token' })),
    )
  })
})
