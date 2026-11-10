import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, expect, it, vi } from 'vitest'
import { ConnectSignInMethodBanner } from '@/components/shared/ConnectSignInMethodBanner'

const get = vi.fn()
vi.mock('@/lib/api', async (importActual) => ({
  ...(await importActual<typeof import('@/lib/api')>()),
  api: { get: (...args: unknown[]) => get(...args) },
}))

const auth = { authenticated: true, role: 'reviewer' }
vi.mock('@/lib/auth', () => ({ useAuth: () => auth }))

function mockApi(identities: unknown[], providers: unknown[]) {
  get.mockImplementation((path: string) =>
    Promise.resolve(path === '/api/user/identities' ? { identities } : { providers }),
  )
}

const PROVIDERS = [{ name: 'google', display_name: 'Google' }]

function renderBanner() {
  return render(
    <MemoryRouter>
      <ConnectSignInMethodBanner />
    </MemoryRouter>,
  )
}

beforeEach(() => {
  auth.authenticated = true
  auth.role = 'reviewer'
  get.mockReset()
  window.sessionStorage.clear()
})

it('suggests connecting a provider when none is linked', async () => {
  mockApi([], PROVIDERS)
  renderBanner()

  expect(await screen.findByText(/Connect Google in/i)).toBeTruthy()
})

it('says nothing when a provider is already linked', async () => {
  mockApi([{ provider: 'github' }], PROVIDERS)
  renderBanner()

  await waitFor(() => expect(get).toHaveBeenCalled())
  expect(screen.queryByText(/Connect Google in/i)).toBeNull()
})

it('says nothing on a deployment without SSO', async () => {
  mockApi([], [])
  renderBanner()

  await waitFor(() => expect(get).toHaveBeenCalled())
  expect(screen.queryByText(/Connect/)).toBeNull()
})

it('does not ask a viewer, who cannot connect one', async () => {
  auth.role = 'viewer'
  mockApi([], PROVIDERS)
  renderBanner()

  // Not just "shows nothing": the viewer's browser never asks the server.
  await waitFor(() => expect(get).not.toHaveBeenCalled())
  expect(screen.queryByText(/Connect Google in/i)).toBeNull()
})

it('does not ask an unauthenticated visitor', async () => {
  auth.authenticated = false
  mockApi([], PROVIDERS)
  renderBanner()

  await waitFor(() => expect(get).not.toHaveBeenCalled())
  expect(screen.queryByText(/Connect Google in/i)).toBeNull()
})

it('stays dismissed for the session', async () => {
  mockApi([], PROVIDERS)
  const first = renderBanner()
  await screen.findByText(/Connect Google in/i)

  await userEvent.click(screen.getByRole('button', { name: /dismiss/i }))
  expect(screen.queryByText(/Connect Google in/i)).toBeNull()
  first.unmount()

  // A fresh mount in the same session stays quiet — and it *did* fetch, so this
  // asserts the stored dismissal rather than a request that never settled.
  get.mockClear()
  renderBanner()
  await waitFor(() => expect(get).toHaveBeenCalled())
  expect(screen.queryByText(/Connect Google in/i)).toBeNull()
})

it('stops prompting when the role drops to viewer while mounted', async () => {
  // The effect skips a viewer, but state set by an earlier render must not keep
  // the prompt on screen for someone whose connect would now be refused.
  mockApi([], PROVIDERS)
  const view = renderBanner()
  await screen.findByText(/Connect Google in/i)

  auth.role = 'viewer'
  view.rerender(
    <MemoryRouter>
      <ConnectSignInMethodBanner />
    </MemoryRouter>,
  )

  await waitFor(() =>
    expect(screen.queryByText(/Connect Google in/i)).toBeNull(),
  )
})
