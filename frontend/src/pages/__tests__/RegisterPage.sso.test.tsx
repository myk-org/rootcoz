import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { RegisterPage } from '@/pages/RegisterPage'

/** A real `ApiError`, in the shape `lib/api` unwraps. */
function apiError(status: number, detail: string) {
  return new apiMock.ApiError(status, 'Error', { detail })
}

const TICKET = {
  provider: 'google',
  suggested_username: 'rnetser',
  email_domain: 'redhat.com',
}

const apiMock = vi.hoisted(() => {
  let ApiError: typeof import('@/lib/api').ApiError
  return {
    // A getter, because the class is only known once the module mock loads.
    get ApiError() {
      return ApiError
    },
    setApiError(cls: typeof import('@/lib/api').ApiError) {
      ApiError = cls
    },
    api: {
      get: vi.fn<(path: string) => Promise<unknown>>(),
      post: vi.fn<(path: string, body?: unknown) => Promise<unknown>>(),
    },
  }
})

vi.mock('@/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api')>()
  // Use the real classes too: `extractApiDetail` checks `instanceof ApiError`,
  // so a local stand-in would silently unwrap nothing.
  apiMock.setApiError(actual.ApiError)
  return {
    api: apiMock.api,
    ApiError: actual.ApiError,
    extractApiDetail: actual.extractApiDetail,
  }
})

const authMock = vi.hoisted(() => ({
  login: vi.fn(async () => {}),
  refreshAuth: vi.fn(async () => {}),
}))

vi.mock('@/lib/auth', () => ({
  useAuth: () => ({
    ...authMock,
    authenticated: false,
    loading: false,
    username: null,
  }),
}))

/** Default server: Google configured, and a live ticket for the given value. */
function defaultServer() {
  apiMock.api.get.mockImplementation(async (path: string) => {
    if (path === '/api/auth/sso/providers') {
      return { providers: [{ name: 'google', display_name: 'Google' }] }
    }
    return {}
  })
  apiMock.api.post.mockImplementation(async (path: string) => {
    if (path === '/api/auth/sso/ticket') return TICKET
    return { username: 'rnetser', status: 'active' }
  })
}

const TICKET_ENTRY = { pathname: '/login', hash: '#sso_ticket=tkt-1' } // pragma: allowlist secret

function renderLogin(entry: string | typeof TICKET_ENTRY = '/login') {
  return render(
    <MemoryRouter initialEntries={[entry]}>
      <RegisterPage />
    </MemoryRouter>,
  )
}

function LocationProbe() {
  const location = useLocation()
  return <div data-testid="location">{`${location.pathname}${location.search}${location.hash}`}</div>
}

/** Same page, but with the destinations mounted so navigation is observable. */
function renderWithDestinations(entry: string | typeof TICKET_ENTRY | Record<string, unknown>) {
  return render(
    <MemoryRouter initialEntries={[entry]}>
      <LocationProbe />
      <Routes>
        <Route path="/login" element={<RegisterPage />} />
        <Route path="/pending" element={<div>pending page</div>} />
        <Route path="/report/1" element={<div>report page</div>} />
      </Routes>
    </MemoryRouter>,
  )
}

describe('RegisterPage SSO providers', () => {
  beforeEach(() => {
    localStorage.clear()
    apiMock.api.get.mockReset()
    apiMock.api.post.mockReset()
    authMock.refreshAuth.mockClear()
    defaultServer()
  })

  it('renders one full-page sign-in link per configured provider', async () => {
    renderLogin()
    const link = await screen.findByRole('link', { name: /^sign in with google$/i })
    expect(link).toHaveAttribute('href', '/api/auth/sso/google/login?next=%2F')
  })

  it('renders no provider link when none are configured', async () => {
    apiMock.api.get.mockImplementation(async () => ({ providers: [] }))
    renderLogin()
    await screen.findByLabelText(/^username$/i)
    await waitFor(() => expect(apiMock.api.get).toHaveBeenCalledWith('/api/auth/sso/providers'))
    expect(screen.queryByRole('link', { name: /sign in with/i })).toBeNull()
  })
})

describe('RegisterPage SSO first-login choice', () => {
  beforeEach(() => {
    localStorage.clear()
    apiMock.api.get.mockReset()
    apiMock.api.post.mockReset()
    authMock.refreshAuth.mockClear()
    defaultServer()
  })

  it('redeems the ticket in the fragment and shows the choice panel instead of the forms', async () => {
    renderLogin(TICKET_ENTRY)
    expect(await screen.findByRole('heading', { name: /this google account isn't connected yet/i })).toBeInTheDocument()
    expect(apiMock.api.post).toHaveBeenCalledWith('/api/auth/sso/ticket', { ticket: 'tkt-1' })
    expect(screen.getByLabelText(/^username$/i)).toHaveValue('rnetser')
    expect(screen.getByText(/@redhat\.com/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /^log in$/i })).toBeNull()
    expect(screen.queryByRole('button', { name: /^register$/i })).toBeNull()
  })

  it("shows the server's message when the chosen name cannot be used", async () => {
    apiMock.api.post.mockImplementation(async (path: string) => {
      if (path === '/api/auth/sso/ticket') return TICKET
      throw apiError(400, 'That name cannot be used')
    })
    renderLogin(TICKET_ENTRY)
    await screen.findByRole('heading', { name: /isn't connected yet/i })

    await userEvent.click(screen.getByRole('button', { name: /^create account$/i }))

    expect(await screen.findByText('That name cannot be used')).toBeInTheDocument()
    expect(apiMock.api.post).toHaveBeenCalledWith('/api/auth/sso/create', {
      ticket: 'tkt-1',
      username: 'rnetser',
    })
  })

  it('reports a dead ticket as expired and offers the way back to the forms', async () => {
    apiMock.api.post.mockImplementation(async (path: string) => {
      if (path === '/api/auth/sso/ticket') {
        throw apiError(404, 'This sign-in link has expired')
      }
      return {}
    })
    renderLogin(TICKET_ENTRY)

    expect(await screen.findByRole('heading', { name: /this sign-in link has expired/i })).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: /back to sign in/i }))
    expect(await screen.findByRole('button', { name: /^log in$/i })).toBeInTheDocument()
  })

  it('gives up the panel and the hash when the person is not who the provider says', async () => {
    renderLogin(TICKET_ENTRY)
    await screen.findByRole('heading', { name: /isn't connected yet/i })

    await userEvent.click(screen.getByRole('button', { name: /sign in another way/i }))

    expect(await screen.findByRole('button', { name: /^log in$/i })).toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: /isn't connected yet/i })).toBeNull()
  })

  it('shows a human message for sso_error=denied, never the raw code', async () => {
    renderLogin('/login?sso_error=denied')
    expect(await screen.findByText("That account can't sign in with this provider.")).toBeInTheDocument()
    expect(screen.queryByText('denied')).toBeNull()
  })

  it('sends a pending account to /pending instead of the requested page', async () => {
    apiMock.api.post.mockImplementation(async (path: string) => {
      if (path === '/api/auth/sso/ticket') return TICKET
      return { username: 'rnetser', status: 'pending' }
    })
    renderWithDestinations(TICKET_ENTRY)
    await screen.findByRole('heading', { name: /isn't connected yet/i })

    await userEvent.click(screen.getByRole('button', { name: /^create account$/i }))

    expect(await screen.findByText('pending page')).toBeInTheDocument()
  })

  it('returns to the requested page after finishing', async () => {
    const entry = {
      pathname: '/login',
      hash: '#sso_ticket=tkt-1', // pragma: allowlist secret
      state: { from: { pathname: '/report/1' } },
    }
    renderWithDestinations(entry)
    await screen.findByRole('heading', { name: /isn't connected yet/i })

    await userEvent.click(screen.getByRole('button', { name: /^create account$/i }))

    expect(await screen.findByText('report page')).toBeInTheDocument()
  })

  it('sends the name the person typed, not the suggestion', async () => {
    renderLogin(TICKET_ENTRY)
    await screen.findByRole('heading', { name: /isn't connected yet/i })

    const nameField = screen.getByLabelText(/^username$/i)
    await userEvent.clear(nameField)
    await userEvent.type(nameField, 'ruthn')
    await userEvent.click(screen.getByRole('button', { name: /^create account$/i }))

    await waitFor(() =>
      expect(apiMock.api.post).toHaveBeenCalledWith('/api/auth/sso/create', {
        ticket: 'tkt-1',
        username: 'ruthn',
      }),
    )
  })

  it('does not let a live session swallow an SSO attempt', async () => {
    // A person who is already signed in but follows a provider link must still
    // see the choice panel: the ticket has to be redeemed, not redirected past.
    renderWithDestinations(TICKET_ENTRY)
    expect(await screen.findByRole('heading', { name: /isn't connected yet/i })).toBeInTheDocument()
    expect(screen.queryByText('report page')).toBeNull()
  })

  it('treats a rejected account as blocked, not "active"', async () => {
    apiMock.api.post.mockImplementation(async (path: string) => {
      if (path === '/api/auth/sso/ticket') return TICKET
      // No `next` in the body on purpose: the SPA's own fallback must still
      // treat a non-active status as blocked.
      return { username: 'rnetser', status: 'rejected' }
    })
    renderWithDestinations(TICKET_ENTRY)
    await screen.findByRole('heading', { name: /isn't connected yet/i })

    await userEvent.click(screen.getByRole('button', { name: /^create account$/i }))

    expect(await screen.findByText('pending page')).toBeInTheDocument()
  })

  it('ignores an inherited key when mapping sso_error', async () => {
    renderLogin('/login?sso_error=__proto__')
    expect(await screen.findByText(/did not finish/i)).toBeInTheDocument()
  })

  it('shows the spinner, not the login form, while the ticket is checked', async () => {
    let resolveTicket: (value: unknown) => void = () => {}
    apiMock.api.post.mockImplementation(
      (path: string) =>
        path === '/api/auth/sso/ticket'
          ? new Promise((resolve) => { resolveTicket = resolve })
          : Promise.resolve({ username: 'rnetser', status: 'active' }),
    )
    renderLogin(TICKET_ENTRY)

    expect(await screen.findByRole('status', { name: /loading/i })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /^log in$/i })).toBeNull()

    resolveTicket(TICKET)
    expect(await screen.findByRole('heading', { name: /isn't connected yet/i })).toBeInTheDocument()
  })

  it('maps an unknown sso_error to the fallback, never the raw code', async () => {
    renderLogin('/login?sso_error=some_new_reason')
    expect(await screen.findByText(/did not finish/i)).toBeInTheDocument()
    expect(screen.queryByText('some_new_reason')).toBeNull()
  })

  it('announces a refusal as an alert', async () => {
    apiMock.api.post.mockImplementation(async (path: string) => {
      if (path === '/api/auth/sso/ticket') return TICKET
      throw apiError(400, 'That name cannot be used')
    })
    renderLogin(TICKET_ENTRY)
    await screen.findByRole('heading', { name: /isn't connected yet/i })

    await userEvent.click(screen.getByRole('button', { name: /^create account$/i }))

    expect(await screen.findByRole('alert')).toHaveTextContent('That name cannot be used')
  })

  it('drops the ticket from the URL when the panel is dismissed', async () => {
    renderWithDestinations(TICKET_ENTRY)
    await screen.findByRole('heading', { name: /isn't connected yet/i })

    await userEvent.click(screen.getByRole('button', { name: /sign in another way/i }))

    expect(await screen.findByTestId('location')).toHaveTextContent(/^\/login$/)
    expect(screen.queryByRole('heading', { name: /isn't connected yet/i })).toBeNull()
  })

  it('does not submit an empty API key to the link endpoint', async () => {
    renderLogin(TICKET_ENTRY)
    await screen.findByRole('heading', { name: /isn't connected yet/i })
    await userEvent.click(screen.getByRole('button', { name: /i already have an account/i }))

    const connect = screen.getByRole('button', { name: /^connect account$/i })
    expect(connect).toBeDisabled()
    expect(apiMock.api.post).not.toHaveBeenCalledWith('/api/auth/sso/link', expect.anything())
  })

  it('treats a ticket that dies mid-submit as expired, not an error', async () => {
    apiMock.api.post.mockImplementation(async (path: string) => {
      if (path === '/api/auth/sso/ticket') return TICKET
      throw apiError(404, 'This sign-in link has expired')
    })
    renderLogin(TICKET_ENTRY)
    await screen.findByRole('heading', { name: /isn't connected yet/i })

    await userEvent.click(screen.getByRole('button', { name: /^create account$/i }))

    expect(await screen.findByRole('heading', { name: /this sign-in link has expired/i })).toBeInTheDocument()
  })

  it('links an existing account with the credentials typed into the panel', async () => {
    renderLogin(TICKET_ENTRY)
    await screen.findByRole('heading', { name: /isn't connected yet/i })

    await userEvent.click(screen.getByRole('button', { name: /i already have an account/i }))
    await userEvent.type(screen.getByLabelText(/^username$/i), 'jdoe')
    await userEvent.type(screen.getByLabelText(/api key/i), 'existing-key') // pragma: allowlist secret
    await userEvent.click(screen.getByRole('button', { name: /^connect account$/i }))

    await waitFor(() =>
      expect(apiMock.api.post).toHaveBeenCalledWith('/api/auth/sso/link', {
        ticket: 'tkt-1',
        username: 'jdoe',
        api_key: 'existing-key', // pragma: allowlist secret
      }),
    )
    await waitFor(() => expect(authMock.refreshAuth).toHaveBeenCalled())
  })
})
