import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter, RouterProvider, createMemoryRouter, useLocation } from 'react-router-dom'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { UsersPage } from '@/pages/UsersPage'

const { get, put, post, ApiError } = vi.hoisted(() => {
  class ApiError extends Error {
    status: number
    body: unknown
    constructor(status: number, body: unknown) {
      super('api error')
      this.status = status
      this.body = body
    }
  }
  return { get: vi.fn(), put: vi.fn(), post: vi.fn(), ApiError }
})

vi.mock('@/lib/api', () => ({ api: { get, put, post, delete: vi.fn() }, ApiError }))
vi.mock('@/lib/auth', () => ({ useAuth: () => ({ username: 'root', refreshAuth: vi.fn() }) }))

// Radix Select pokes at pointer-capture and scrolling APIs jsdom does not have.
HTMLElement.prototype.hasPointerCapture = () => false
HTMLElement.prototype.setPointerCapture = () => {}
HTMLElement.prototype.releasePointerCapture = () => {}
HTMLElement.prototype.scrollIntoView = () => {}

const user = (username: string, extra: Record<string, unknown> = {}) => ({
  username,
  role: 'reviewer',
  status: 'active',
  can_use_server_providers: false,
  can_view_reports: false,
  created_at: '',
  last_seen: null,
  locked: false,
  locked_at: null,
  disabled: false,
  failed_login_count: 0,
  ...extra,
})

// Fake server for GET /api/admin/users: it honours limit/offset/q and counts the
// whole match, so the page's range is checked against real arithmetic. The
// lockout threshold is a Server Setting, asked for only when a row is locked.
const LOCKOUT_HINT_THRESHOLD = '5'
/** The page's debounce, so a search test can wait exactly past it. */
const SEARCH_DEBOUNCE_MS = 300

function mockServer(all: Array<{ username: string }>) {
  get.mockImplementation(async (url: string) => {
    if (url === '/api/admin/settings') {
      return [{ key: 'login_lockout_threshold', value: LOCKOUT_HINT_THRESHOLD }]
    }
    const params = new URL(url, 'http://localhost').searchParams
    const limit = Number(params.get('limit'))
    const offset = Number(params.get('offset'))
    const q = (params.get('q') ?? '').toLowerCase()
    const matched = q ? all.filter((u) => u.username.toLowerCase().includes(q)) : all
    return { users: matched.slice(offset, offset + limit), total: matched.length, limit, offset }
  })
}

const mockUsers = (users: Array<{ username: string }>) => mockServer(users)

/** Every request the page made for rows, in order. */
const usersRequests = () =>
  get.mock.calls.map(([url]) => String(url)).filter((url) => url.startsWith('/api/admin/users?'))

/** 137 accounts named user-001…user-137, so a range has something to count. */
const manyUsers = (count: number) =>
  Array.from({ length: count }, (_, i) => user(`user-${String(i + 1).padStart(3, '0')}`))

function renderPage(entry = '/admin/users') {
  return render(
    <MemoryRouter initialEntries={[entry]}>
      <UsersPage />
    </MemoryRouter>,
  )
}

function LocationProbe() {
  const location = useLocation()
  return <span data-testid="location">{`${location.pathname}${location.search}`}</span>
}

/** A router whose history stack the test can walk, like a real Back button. */
function renderRouted(initialEntries: string[], initialIndex = 0) {
  const router = createMemoryRouter(
    [
      {
        path: '/admin/users',
        element: (
          <>
            <UsersPage />
            <LocationProbe />
          </>
        ),
      },
    ],
    { initialEntries, initialIndex },
  )
  const view = render(<RouterProvider router={router} />)
  return { router, ...view }
}

beforeEach(() => {
  get.mockReset()
  put.mockReset()
  post.mockReset()
  mockUsers([user('alice'), user('bob')])
})

describe('UsersPage search', () => {
  it('pre-fills the search from ?user= so the audit link lands on that account', async () => {
    renderPage('/admin/users?user=alice')

    expect(await screen.findByText('alice')).toBeInTheDocument()
    expect(screen.getByLabelText('Search users')).toHaveValue('alice')
    expect(screen.queryByText('bob')).not.toBeInTheDocument()
    // The name is a server-side filter, so the count describes the match only.
    expect(get).toHaveBeenCalledWith('/api/admin/users?limit=25&offset=0&q=alice')
  })

  it('lists every user when the page is opened without the param', async () => {
    renderPage()

    expect(await screen.findByText('alice')).toBeInTheDocument()
    expect(screen.getByText('bob')).toBeInTheDocument()
    expect(screen.getByLabelText('Search users')).toHaveValue('')
  })
})

describe('UsersPage pagination', () => {
  it('asks for the first window and reports the range from the server total', async () => {
    mockServer(manyUsers(137))
    renderPage()

    expect(await screen.findByText('user-001')).toBeInTheDocument()
    expect(get).toHaveBeenCalledWith('/api/admin/users?limit=25&offset=0')
    // Header row plus exactly one page of accounts.
    expect(screen.getAllByRole('row')).toHaveLength(26)
    expect(screen.getByRole('status')).toHaveTextContent('1–25 of 137')
  })

  it('moves to the next window and back, and stops at both ends', async () => {
    mockServer(manyUsers(137))
    const user1 = userEvent.setup()
    renderPage()

    expect(await screen.findByRole('button', { name: 'Previous' })).toBeDisabled()

    await user1.click(screen.getByRole('button', { name: 'Next' }))
    expect(await screen.findByText('user-026')).toBeInTheDocument()
    expect(get).toHaveBeenCalledWith('/api/admin/users?limit=25&offset=25')
    expect(screen.getByRole('status')).toHaveTextContent('26–50 of 137')

    await user1.click(screen.getByRole('button', { name: 'Previous' }))
    expect(await screen.findByText('user-001')).toBeInTheDocument()
    expect(get).toHaveBeenCalledWith('/api/admin/users?limit=25&offset=0')
    expect(screen.getByRole('status')).toHaveTextContent('1–25 of 137')
  })

  it('disables Next on the last window', async () => {
    mockServer(manyUsers(60))
    renderPage('/admin/users?limit=25&offset=50')

    expect(await screen.findByText('user-051')).toBeInTheDocument()
    expect(screen.getByRole('status')).toHaveTextContent('51–60 of 60')
    expect(screen.getByRole('button', { name: 'Next' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Previous' })).toBeEnabled()
  })

  it('honours ?limit=50&offset=50 on mount', async () => {
    mockServer(manyUsers(137))
    renderPage('/admin/users?limit=50&offset=50')

    expect(await screen.findByText('user-051')).toBeInTheDocument()
    expect(get).toHaveBeenCalledWith('/api/admin/users?limit=50&offset=50')
    expect(screen.getByRole('status')).toHaveTextContent('51–100 of 137')
  })

  it('adopts a Back navigation instead of rewriting the search it left', async () => {
    mockUsers([user('alice'), user('bob')])
    // The browser stack: bob first, then the page was opened on alice.
    const { router } = renderRouted(['/admin/users?q=bob', '/admin/users?q=alice'], 1)

    expect(await screen.findByText('alice')).toBeInTheDocument()
    expect(get).toHaveBeenCalledWith('/api/admin/users?limit=25&offset=0&q=alice')

    await act(async () => {
      await router.navigate(-1)
    })

    // The input follows the URL and the request follows the input.
    await waitFor(() => expect(screen.getByLabelText('Search users')).toHaveValue('bob'))
    expect(await screen.findByText('bob')).toBeInTheDocument()
    expect(get).toHaveBeenCalledWith('/api/admin/users?limit=25&offset=0&q=bob')
    // ...and the debounce never writes the previous needle back over it.
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, SEARCH_DEBOUNCE_MS + 50))
    })
    expect(screen.getByTestId('location')).toHaveTextContent('/admin/users?q=bob')
    expect(screen.getByLabelText('Search users')).toHaveValue('bob')
    expect(usersRequests().some((url) => url.endsWith('q=alice'))).toBe(true)
    expect(usersRequests().at(-1)).toContain('q=bob')
  })

  it('ignores a page that resolves after the admin already moved on', async () => {
    // Two requests in flight; the older one must not paint over the newer.
    const pending: Array<{ resolve: (value: unknown) => void }> = []
    get.mockImplementation(() =>
      new Promise((resolve) => {
        pending.push({ resolve })
      }),
    )
    vi.useFakeTimers()
    try {
      renderPage()
      await act(async () => { await vi.advanceTimersByTimeAsync(0) })
      expect(pending).toHaveLength(1)

      fireEvent.change(screen.getByLabelText('Search users'), { target: { value: 'alice' } })
      await act(async () => { await vi.advanceTimersByTimeAsync(SEARCH_DEBOUNCE_MS) })
      expect(pending).toHaveLength(2)

      const [stale, fresh] = pending
      await act(async () => {
        fresh.resolve({ users: [user('alice')], total: 1, limit: 25, offset: 0 })
        await vi.advanceTimersByTimeAsync(0)
      })
      expect(screen.getByText('alice')).toBeInTheDocument()

      await act(async () => {
        stale.resolve({ users: [user('bob')], total: 1, limit: 25, offset: 0 })
        await vi.advanceTimersByTimeAsync(0)
      })
      // The newest answer stands: no row for a search the admin already left.
      expect(screen.queryByText('bob')).not.toBeInTheDocument()
      expect(screen.getByText('alice')).toBeInTheDocument()
      expect(screen.getByRole('status')).toHaveTextContent('1–1 of 1')
    } finally {
      vi.useRealTimers()
    }
  })

  it('starts over at the first window when the page size changes', async () => {
    mockServer(manyUsers(137))
    const user1 = userEvent.setup()
    renderPage('/admin/users?limit=25&offset=25')

    await screen.findByText('user-026')
    await user1.click(screen.getByRole('combobox', { name: 'Rows per page' }))
    await user1.click(await screen.findByRole('option', { name: '50' }))

    expect(get).toHaveBeenCalledWith('/api/admin/users?limit=50&offset=0')
    expect(await screen.findByText('user-001')).toBeInTheDocument()
    expect(screen.getByRole('status')).toHaveTextContent('1–50 of 137')
  })

  it('debounces the search into q and returns to the first window', async () => {
    mockServer(manyUsers(137))
    // Timers are the subject here: a keystroke must not reach the API until it stops.
    vi.useFakeTimers()
    try {
      renderPage('/admin/users?limit=50&offset=50')
      await act(async () => { await vi.advanceTimersByTimeAsync(0) })
      expect(screen.getByText('user-051')).toBeInTheDocument()

      fireEvent.change(screen.getByLabelText('Search users'), { target: { value: 'user-13' } })
      await act(async () => { await vi.advanceTimersByTimeAsync(299) })
      expect(usersRequests().filter((url) => url.includes('q='))).toHaveLength(0)

      await act(async () => { await vi.advanceTimersByTimeAsync(1) })
      expect(get).toHaveBeenCalledWith('/api/admin/users?limit=50&offset=0&q=user-13')
      expect(usersRequests().filter((url) => url.includes('q='))).toHaveLength(1)
      // The count now describes the match, not the unfiltered list.
      expect(screen.getByRole('status')).toHaveTextContent('1–8 of 8')
      expect(screen.getByText('user-137')).toBeInTheDocument()
    } finally {
      vi.useRealTimers()
    }
  })
})

describe('UsersPage account state', () => {
  it('shows active, disabled and locked state in the row without opening anything', async () => {
    mockUsers([
      user('alice'),
      user('bob', { disabled: true }),
      user('carol', { locked: true, locked_at: '2025-01-01T00:00:00Z', failed_login_count: 3 }),
    ])
    renderPage()

    const activeRow = (await screen.findByText('alice')).closest('tr')!
    const activeBadge = within(activeRow).getByText('active')
    expect(activeBadge).toHaveClass('text-signal-green')

    // Disabled reads as disabled two ways: the badge says so and the name is muted.
    const disabledRow = screen.getByText('bob').closest('tr')!
    const disabledBadge = within(disabledRow).getByText('disabled')
    expect(disabledBadge).toHaveClass('text-text-tertiary')
    expect(screen.getByText('bob')).toHaveClass('text-text-tertiary')

    // The denominator is the live login_lockout_threshold setting.
    const lockedRow = screen.getByText('carol').closest('tr')!
    // The threshold arrives on its own request, so wait for it rather than race it.
    const lockedBadge = await within(lockedRow).findByText('locked (3/5)')
    expect(lockedBadge).toHaveClass('text-signal-amber')
    expect(get).toHaveBeenCalledWith('/api/admin/settings')

    // Same box in every state, so a badge changing never moves the row.
    for (const badge of [activeBadge, disabledBadge, lockedBadge]) {
      expect(badge).toHaveClass('px-2', 'py-0.5', 'text-xs')
    }
  })

  it('does not read the lockout setting when no row is locked', async () => {
    renderPage()
    await screen.findByText('alice')

    expect(get).not.toHaveBeenCalledWith('/api/admin/settings')
  })

  it('shows a disabled account as disabled whatever its registration status says', async () => {
    mockUsers([
      user('dana', { status: 'pending', disabled: true }),
      user('evan', { status: 'rejected', disabled: true }),
    ])
    renderPage()

    const dana = (await screen.findByText('dana')).closest('tr')!
    expect(within(dana).getByText('disabled')).toHaveClass('text-text-tertiary')
    expect(within(dana).queryByText('pending')).not.toBeInTheDocument()

    const evan = screen.getByText('evan').closest('tr')!
    expect(within(evan).getByText('disabled')).toHaveClass('text-text-tertiary')
    expect(within(evan).queryByText('rejected')).not.toBeInTheDocument()
    // The name is muted from the same answer the badge reads.
    expect(screen.getByText('dana')).toHaveClass('text-text-tertiary')
  })

  it('still shows a locked account as locked over a stale registration status', async () => {
    mockUsers([user('carol', { status: 'active', locked: true, failed_login_count: 3 })])
    renderPage()

    const carol = (await screen.findByText('carol')).closest('tr')!
    expect(await within(carol).findByText('locked (3/5)')).toBeInTheDocument()
    expect(screen.getByText('carol')).toHaveClass('text-text-primary')
  })

  it('disables only after confirmation, then refetches instead of patching the row', async () => {
    put.mockResolvedValue({})
    const user1 = userEvent.setup()
    renderPage()

    await user1.click(await screen.findByRole('button', { name: 'Disable alice' }))
    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).getByText(/cannot log in until you enable/i)).toBeInTheDocument()
    expect(put).not.toHaveBeenCalled()

    // The server is the truth: the row flips only because the refetch says so.
    mockUsers([user('alice', { disabled: true }), user('bob')])
    await user1.click(within(dialog).getByRole('button', { name: 'Disable' }))

    await waitFor(() =>
      expect(put).toHaveBeenCalledWith('/api/admin/users/alice/disabled', { disabled: true }),
    )
    expect(await screen.findByRole('button', { name: 'Enable alice' })).toBeInTheDocument()
    expect(screen.getByText('disabled')).toBeInTheDocument()
  })

  it('enables immediately, without asking for confirmation', async () => {
    mockUsers([user('alice', { disabled: true })])
    put.mockResolvedValue({})
    const user1 = userEvent.setup()
    renderPage()

    await user1.click(await screen.findByRole('button', { name: 'Enable alice' }))

    await waitFor(() =>
      expect(put).toHaveBeenCalledWith('/api/admin/users/alice/disabled', { disabled: false }),
    )
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  })

  it('offers Unlock only on a locked row and calls the unlock endpoint', async () => {
    mockUsers([user('alice'), user('carol', { locked: true, failed_login_count: 3 })])
    post.mockResolvedValue({})
    const user1 = userEvent.setup()
    renderPage()

    expect(await screen.findByRole('button', { name: 'Unlock carol' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Unlock alice' })).not.toBeInTheDocument()

    await user1.click(screen.getByRole('button', { name: 'Unlock carol' }))

    await waitFor(() => expect(post).toHaveBeenCalledWith('/api/admin/users/carol/unlock'))
    // Unlock is not destructive-leaning: no confirmation step.
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  })

  it('surfaces a failed unlock inline and keeps the row', async () => {
    mockUsers([user('carol', { locked: true, failed_login_count: 3 })])
    post.mockRejectedValue(new ApiError(409, { detail: 'Account is not locked' }))
    const user1 = userEvent.setup()
    renderPage()

    await user1.click(await screen.findByRole('button', { name: 'Unlock carol' }))

    expect(await screen.findByRole('alert')).toHaveTextContent('Account is not locked')
    expect(screen.getByText('carol')).toBeInTheDocument()
    expect(screen.getByText('locked (3/5)')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Unlock carol' })).toBeInTheDocument()
  })
})
