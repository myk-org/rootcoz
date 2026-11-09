import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, useLocation } from 'react-router-dom'
import { afterEach, expect, it, vi } from 'vitest'
import { AuthProvider } from '@/lib/AuthProvider'
import * as cookies from '@/lib/cookies'
import { clearUsername } from '@/lib/cookies'
import { hardNavigate } from '@/lib/navigation'
import { useSignOut } from '@/lib/useSignOut'

const get = vi.fn()
const post = vi.fn()
vi.mock('@/lib/navigation', () => ({ hardNavigate: vi.fn(() => true) }))
vi.mock('@/lib/api', async (importActual) => ({
  ...(await importActual<typeof import('@/lib/api')>()),
  api: {
    get: (...args: unknown[]) => get(...args),
    post: (...args: unknown[]) => post(...args),
  },
}))

function LocationProbe() {
  const location = useLocation()
  return <div data-testid="location">{location.pathname}</div>
}

/** The real caller: the sign-out hook owns the single navigation decision. */
function SignOutButton() {
  const signOut = useSignOut()
  return <button onClick={() => void signOut()}>Log out</button>
}

function renderPage() {
  return render(
    <AuthProvider>
      <MemoryRouter initialEntries={['/']}>
        <LocationProbe />
        <SignOutButton />
      </MemoryRouter>
    </AuthProvider>,
  )
}

afterEach(() => {
  get.mockReset()
  post.mockReset()
  clearUsername()
  vi.mocked(hardNavigate).mockClear()
  vi.restoreAllMocks()
})

function signedIn() {
  get.mockResolvedValue({
    username: 'alice',
    role: 'reviewer',
    is_admin: false,
    can_use_server_providers: false,
  })
}

it('follows the IdP logout URL when the server reports one', async () => {
  signedIn()
  post.mockImplementation(async (path: string) =>
    path === '/api/auth/logout'
      ? { ok: true, idp_logout_url: 'https://sso.example.com/logout?idp=1' }
      : {},
  )
  renderPage()

  await userEvent.click(screen.getByRole('button', { name: /log out/i }))

  await waitFor(() =>
    expect(hardNavigate).toHaveBeenCalledWith('https://sso.example.com/logout?idp=1'),
  )
  // Exactly one navigation: the IdP's, not the login form.
  expect(screen.getByTestId('location')).toHaveTextContent(/^\/$/)
  // And the local state was dropped before the browser left.
  expect(cookies.getUsername()).toBe('')
})

it('lands on the login form when the server reports no IdP session', async () => {
  signedIn()
  post.mockImplementation(async (path: string) =>
    path === '/api/auth/logout' ? { ok: true, idp_logout_url: null } : {},
  )
  renderPage()

  await userEvent.click(screen.getByRole('button', { name: /log out/i }))

  await waitFor(() =>
    expect(screen.getByTestId('location')).toHaveTextContent(/^\/login$/),
  )
  expect(hardNavigate).not.toHaveBeenCalled()
})

it('falls back to the login form when the browser guard refuses the URL', async () => {
  signedIn()
  post.mockImplementation(async (path: string) =>
    path === '/api/auth/logout'
      ? { ok: true, idp_logout_url: 'https://sso.example.com/logout' }
      : {},
  )
  vi.mocked(hardNavigate).mockReturnValueOnce(false)
  renderPage()

  await userEvent.click(screen.getByRole('button', { name: /log out/i }))

  // A cleared session must never leave the person stranded on the old page.
  await waitFor(() =>
    expect(screen.getByTestId('location')).toHaveTextContent(/^\/login$/),
  )
})
