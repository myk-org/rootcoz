import { render, screen, waitFor, within } from '@testing-library/react'
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

function mockState(
  identities: unknown[],
  unlinkable: boolean,
  browserLogin = true,
) {
  get.mockImplementation((path: string) => {
    if (path === '/api/user/identities') {
      return Promise.resolve({ identities, unlinkable })
    }
    return Promise.resolve({
      browser_login: browserLogin,
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
  mockState([{ provider: 'google', display_name: 'Google', email: 'a@example.com', created_at: '' }], true)
  render(<SignInMethodsCard />)

  expect(await screen.findByText('Google')).toBeTruthy()
  expect(screen.getByText('a@example.com')).toBeTruthy()
})

it('uses server-supplied display_name without a hardcoded provider map', async () => {
  mockState([{ provider: 'corp-oidc', display_name: 'Corp SSO', email: 'a@corp.example', created_at: '' }], true)
  render(<SignInMethodsCard />)

  expect(await screen.findByText('Corp SSO')).toBeTruthy()
})

it('says no provider is connected when the list is empty', async () => {
  mockState([], true)
  render(<SignInMethodsCard />)

  expect(
    await screen.findByText(/no provider is connected yet/i),
  ).toBeTruthy()
  expect(screen.queryByText(/loading/i)).toBeNull()
})

it('reports a failed load without also claiming to be loading', async () => {
  get.mockImplementation((path: string) =>
    path === '/api/user/identities'
      ? Promise.reject(new Error('network down'))
      : Promise.resolve({
          browser_login: true,
          providers: [{ name: 'github', display_name: 'GitHub' }],
        }),
  )
  render(<SignInMethodsCard />)

  expect(
    await screen.findByText(/could not load your sign-in methods/i),
  ).toBeTruthy()
  // "Loading…" promises something is still coming; after a failure nothing is.
  expect(screen.queryByText(/loading/i)).toBeNull()
  // Connect state is unknown until the list arrives.
  expect(screen.queryByRole('button', { name: /connect/i })).toBeNull()
  expect(screen.queryByText(/no provider is connected yet/i)).toBeNull()
})

it('offers to connect a provider that is not connected yet', async () => {
  mockState([{ provider: 'google', email: null, created_at: '' }], true)
  render(<SignInMethodsCard />)

  // A real same-origin POST navigation, not a link: the security property is the
  // POST itself, which a cross-site navigation cannot trigger.
  const submit = await screen.findByRole('button', { name: /connect github/i })
  const form = submit.closest('form')
  expect(form?.method).toBe('post')
  expect(form?.getAttribute('action')).toBe(
    '/api/auth/sso/github/login?mode=link&next=%2Fsettings',
  )
  expect(submit.getAttribute('type')).toBe('submit')
  expect(screen.queryByRole('link', { name: /connect github/i })).toBeNull()
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
  expect(screen.getByText(/ask an admin to issue an API key/i)).toBeTruthy()
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

it('offers no connect button when the server has no browser sign-in', async () => {
  // `browser_login: false` means no public origin, so the link start answers 400
  // after the click. The provider is still configured — it is just CLI-only.
  mockState([], true, false)
  render(<SignInMethodsCard />)

  expect(
    await screen.findByText(/browser sign-in is turned off/i),
  ).toBeTruthy()
  expect(screen.getByText('rootcoz auth sso')).toBeTruthy()
  expect(screen.queryByRole('button', { name: /connect/i })).toBeNull()
  expect(screen.queryByRole('link', { name: /connect/i })).toBeNull()
})

it('offers the connect button when the server allows browser sign-in', async () => {
  mockState([], true, true)
  render(<SignInMethodsCard />)

  expect(
    await screen.findByRole('button', { name: /connect github/i }),
  ).toBeTruthy()
  expect(screen.queryByText(/browser sign-in is turned off/i)).toBeNull()
})

it('opens a confirmation dialog when disconnecting the only SSO provider', async () => {
  mockState([{ provider: 'google', display_name: 'Google', email: 'a@example.com', created_at: '' }], true)
  render(<SignInMethodsCard />)

  const btn = await screen.findByRole('button', { name: /disconnect/i })
  await userEvent.click(btn)

  // Dialog appears with the last-SSO warning
  const dialog = screen.getByRole('dialog')
  expect(dialog).toBeTruthy()
  expect(screen.getByText(/Disconnect Google\?/)).toBeTruthy()
  expect(screen.getByText(/only connected SSO provider/i)).toBeTruthy()
  expect(screen.getByText(/RootCoz API key/)).toBeTruthy()
  // Real Unicode punctuation renders — never literal JSX \uXXXX escapes
  expect(dialog.textContent).toContain('API key — make sure')
  expect(dialog.textContent).toContain('don’t get locked out')
  expect(dialog.textContent).not.toContain('\\u')
  // api.delete was NOT called yet
  expect(del).not.toHaveBeenCalled()
})

it('cancelling the last-SSO confirmation leaves the provider linked', async () => {
  mockState([{ provider: 'google', email: null, created_at: '' }], true)
  render(<SignInMethodsCard />)

  await userEvent.click(await screen.findByRole('button', { name: /disconnect/i }))
  const dialog = screen.getByRole('dialog')
  await userEvent.click(within(dialog).getByRole('button', { name: /cancel/i }))

  await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
  expect(del).not.toHaveBeenCalled()
})

it('confirming the last-SSO dialog calls api.delete and closes the dialog', async () => {
  mockState([{ provider: 'google', email: null, created_at: '' }], true)
  del.mockResolvedValue({ ok: true })
  render(<SignInMethodsCard />)

  await userEvent.click(await screen.findByRole('button', { name: /disconnect/i }))
  const dialog = screen.getByRole('dialog')
  await userEvent.click(within(dialog).getByRole('button', { name: /^disconnect$/i }))

  await waitFor(() => expect(del).toHaveBeenCalledWith('/api/user/identities/google'))
})

it('disconnects directly (no dialog) when user has 2+ SSO providers', async () => {
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

  // No dialog — api.delete called immediately
  expect(screen.queryByRole('dialog')).toBeNull()
  expect(del).toHaveBeenCalledWith('/api/user/identities/google')
})
