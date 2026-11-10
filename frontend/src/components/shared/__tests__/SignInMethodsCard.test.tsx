import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, expect, it, vi } from 'vitest'
import { SignInMethodsCard } from '@/components/shared/SignInMethodsCard'

const get = vi.fn()
const del = vi.fn()
const role = { value: 'reviewer' }
vi.mock('@/lib/auth', () => ({
  useAuth: () => ({ role: role.value }),
}))
vi.mock('@/lib/api', async (importActual) => ({
  ...(await importActual<typeof import('@/lib/api')>()),
  api: {
    get: (...args: unknown[]) => get(...args),
    delete: (...args: unknown[]) => del(...args),
  },
}))

function mockState(identities: unknown[], unlinkable: boolean) {
  get.mockImplementation((path: string) => {
    if (path === '/api/user/identities') {
      return Promise.resolve({ identities, unlinkable })
    }
    return Promise.resolve({
      providers: [{ name: 'github', display_name: 'GitHub' }],
    })
  })
}

beforeEach(() => {
  role.value = 'reviewer'
  get.mockReset()
  del.mockReset()
  window.history.replaceState({}, '', '/settings')
})

it('lists the connected providers', async () => {
  mockState([{ provider: 'google', email: 'a@example.com', created_at: '' }], true)
  render(<SignInMethodsCard />)

  expect(await screen.findByText('Google')).toBeTruthy()
  expect(screen.getByText('a@example.com')).toBeTruthy()
})

it('offers to connect a provider that is not connected yet', async () => {
  mockState([{ provider: 'google', email: null, created_at: '' }], true)
  render(<SignInMethodsCard />)

  const link = await screen.findByRole('link', { name: /connect github/i })
  expect(link.getAttribute('href')).toBe(
    '/api/auth/sso/github/login?mode=link&next=%2Fsettings',
  )
})

it('cannot disconnect the only sign-in method', async () => {
  mockState([{ provider: 'google', email: null, created_at: '' }], false)
  render(<SignInMethodsCard />)

  await waitFor(() =>
    expect(
      (screen.getByRole('button', { name: /disconnect/i }) as HTMLButtonElement)
        .disabled,
    ).toBe(true),
  )
  expect(screen.getByText(/only sign-in method/i)).toBeTruthy()
})

it('disconnects a provider and reloads the list', async () => {
  mockState(
    [
      { provider: 'google', email: null, created_at: '' },
      { provider: 'github', email: null, created_at: '' },
    ],
    true,
  )
  del.mockResolvedValue({ ok: true })
  render(<SignInMethodsCard />)

  const [first] = await screen.findAllByRole('button', { name: /disconnect/i })
  await userEvent.click(first)
  expect(del).toHaveBeenCalledWith('/api/user/identities/google')
})

it('does not offer a viewer the actions the server refuses', async () => {
  role.value = 'viewer'
  mockState([{ provider: 'google', email: null, created_at: '' }], true)
  render(<SignInMethodsCard />)

  await waitFor(() =>
    expect(
      (screen.getByRole('button', { name: /disconnect/i }) as HTMLButtonElement)
        .disabled,
    ).toBe(true),
  )
  expect(screen.getByText(/reviewer account is required/i)).toBeTruthy()
  expect(screen.queryByRole('link', { name: /connect/i })).toBeNull()
})

it('reports why a connect attempt came back refused', async () => {
  mockState([], true)
  window.history.replaceState({}, '', '/settings?sso_error=identity_conflict')
  render(<SignInMethodsCard />)

  expect(
    await screen.findByText(/already connected to a different user/i),
  ).toBeTruthy()
})
