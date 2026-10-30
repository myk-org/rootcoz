import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { useEffect } from 'react'
import { MemoryRouter, RouterProvider, createMemoryRouter, useLocation } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { AdminAuditPage } from '@/pages/AdminAuditPage'
import { presetSince } from '@/lib/auditFilters'
import { ApiError } from '@/lib/api'
import { clearTokens, clearUsername, getGithubToken, getUsername, setGithubToken, setUsername } from '@/lib/cookies'
import { formatTimestamp } from '@/lib/utils'

const apiGet = vi.fn()
vi.mock('@/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api')>()
  return { ...actual, api: { get: (...args: unknown[]) => apiGet(...args) } }
})

const logout = vi.hoisted(() => vi.fn<() => Promise<void>>())
vi.mock('@/lib/auth', () => ({ useAuth: () => ({ logout }) }))

// Radix Select opens its listbox through pointer capture, which jsdom lacks.
HTMLElement.prototype.hasPointerCapture = () => false
HTMLElement.prototype.setPointerCapture = () => {}
HTMLElement.prototype.releasePointerCapture = () => {}
HTMLElement.prototype.scrollIntoView = () => {}

const ACTIONS = [
  { action: 'settings.update', label: 'Settings updated', count: 2, last_seen: '2024-01-02 10:00:00' },
  { action: 'user.role.change', label: 'Role changed', count: 1, last_seen: '2024-01-02 10:00:00' },
]

function event(overrides: Record<string, unknown> = {}) {
  return {
    id: 1,
    created_at: '2024-01-02 10:00:00',
    actor: 'alice',
    actor_role: 'admin',
    is_admin: true,
    action: 'settings.update',
    target_type: 'setting',
    target_id: 'ai_call_timeout',
    target_label: null,
    outcome: 'success',
    details: { key: 'ai_call_timeout', changed: true },
    request_ip: '10.0.0.7',
    ...overrides,
  }
}

class MockEventSource {
  static instances: MockEventSource[] = []
  url: string
  closed = false
  onopen: (() => void) | null = null
  onerror: (() => void) | null = null
  private listeners = new Map<string, Array<(e: MessageEvent) => void>>()

  constructor(url: string) {
    this.url = url
    MockEventSource.instances.push(this)
  }

  addEventListener(name: string, handler: (e: MessageEvent) => void) {
    const current = this.listeners.get(name) ?? []
    this.listeners.set(name, [...current, handler])
  }

  removeEventListener(name: string, handler: (e: MessageEvent) => void) {
    this.listeners.set(
      name,
      (this.listeners.get(name) ?? []).filter((h) => h !== handler),
    )
  }

  close() {
    this.closed = true
  }

  emit(name: string, data: unknown) {
    for (const handler of this.listeners.get(name) ?? []) {
      handler(new MessageEvent(name, { data: JSON.stringify(data) }))
    }
  }
}

/** Serve one page of events for whatever limit/offset the page asks for. */
function mockAuditApi(
  total: number,
  build: (offset: number, limit: number) => unknown[] = () => [event()],
) {
  apiGet.mockImplementation((path: string) => {
    if (path.startsWith('/api/admin/audit/actions')) {
      return Promise.resolve({ actions: ACTIONS })
    }
    const params = new URL(path, 'http://x').searchParams
    const offset = Number(params.get('offset') ?? 0)
    const limit = Number(params.get('limit') ?? 50)
    const events = offset < total ? build(offset, limit) : []
    return Promise.resolve({ events, total, limit, offset })
  })
}

/** One event per row, so the range text reflects the requested page size. */
function pageOfEvents(total: number) {
  return (offset: number, limit: number) =>
    Array.from({ length: Math.max(0, Math.min(limit, total - offset)) }, (_, i) =>
      event({ id: offset + i + 1 }),
    )
}

function renderPage(initialEntry = '/admin/audit') {
  return render(
    <MemoryRouter initialEntries={[initialEntry]}>
      <AdminAuditPage />
      <LocationProbe />
    </MemoryRouter>,
  )
}

/** A router whose history stack the test can walk, like a real Back button. */
function renderRouted(initialEntries: string[], initialIndex?: number) {
  const router = createMemoryRouter(
    [
      {
        path: '/admin/audit',
        element: (
          <>
            <AdminAuditPage />
            <LocationProbe />
          </>
        ),
      },
      // The layout has this route; the tests only need somewhere to land.
      { path: '/login', element: <LocationProbe /> },
    ],
    { initialEntries, initialIndex },
  )
  const view = render(<RouterProvider router={router} />)
  return { router, ...view }
}

/** Every location the page has rendered, to catch stray history entries. */
let locationKeys: string[] = []

/** The page owns the query string, so the URL assertions read it from the router. */
function LocationProbe() {
  const location = useLocation()
  useEffect(() => {
    locationKeys.push(location.key)
  }, [location.key])
  return <span data-testid="location">{`${location.pathname}${location.search}`}</span>
}

function liveSwitch() {
  return screen.getByRole('switch', { name: /Live tail/ })
}

function liveDot() {
  return screen.getByTestId('live-dot')
}

/** Tooltip text for the current live phase (Radix renders it on hover). */
async function liveTooltip(): Promise<string> {
  await userEvent.hover(screen.getByTestId('live-status'))
  return (await screen.findByRole('tooltip')).textContent ?? ''
}

/** Type in the actor box and wait for the debounced request it produces. */
async function setActor(value: string, offset = 0) {
  fireEvent.change(screen.getByLabelText('Actor filter'), { target: { value } })
  const suffix = value ? `&actor=${value}` : ''
  await waitFor(() =>
    expect(apiGet).toHaveBeenLastCalledWith(`/api/admin/audit?limit=50&offset=${offset}${suffix}`),
  )
}

function enableLive() {
  const liveSwitch = screen.getByRole('switch', { name: /Live tail/ })
  fireEvent.click(liveSwitch)
  return { liveSwitch, source: MockEventSource.instances[MockEventSource.instances.length - 1] }
}
describe('AdminAuditPage', () => {
  beforeEach(() => {
    apiGet.mockReset()
    logout.mockReset().mockResolvedValue(undefined)
    MockEventSource.instances = []
    locationKeys = []
    clearUsername()
    clearTokens()
    vi.stubGlobal('EventSource', MockEventSource)
  })

  afterEach(() => {
    vi.useRealTimers()
    vi.unstubAllGlobals()
  })

  it('renders rows with actor, action label, target and outcome', async () => {
    mockAuditApi(1)
    renderPage()

    expect(await screen.findByText('alice')).toBeInTheDocument()
    expect(screen.getByText('Settings updated')).toBeInTheDocument()
    expect(screen.getByText('ai_call_timeout')).toBeInTheDocument()
    expect(screen.getByText('success')).toBeInTheDocument()
    expect(screen.getByText('admin')).toBeInTheDocument()
  })

  it('links a job target under its label and keeps the raw id in the tooltip', async () => {
    mockAuditApi(1, () => [
      event({
        target_type: 'job',
        target_id: 'job-42',
        target_job_id: 'job-42',
        target_label: 'e2e-gcp-ovn-42',
        details: { job_id: 'job-42' },
      }),
    ])
    renderPage()

    const link = await screen.findByRole('link', { name: 'e2e-gcp-ovn-42' })
    expect(link).toHaveAttribute('href', '/results/job-42')
    // The id stays reachable (and copyable) even though the label is the text.
    expect(link).toHaveClass('select-text')
    await userEvent.hover(link)
    expect((await screen.findByRole('tooltip')).textContent).toBe('job:job-42')
  })

  it('links a job named by target_id through the resolved job id', async () => {
    mockAuditApi(1, () => [
      event({
        // Metadata writes store the job name, not a uuid: the name is not a path.
        target_type: 'job',
        target_id: 'file-analysis-1',
        target_job_id: 'uuid-1',
        target_label: 'file-analysis-1',
      }),
    ])
    renderPage()

    const link = await screen.findByRole('link', { name: 'file-analysis-1' })
    expect(link).toHaveAttribute('href', '/results/uuid-1')
    await userEvent.hover(link)
    expect((await screen.findByRole('tooltip')).textContent).toBe('job:file-analysis-1')
  })

  it('links a live event by its own uuid while the job id is unresolved', async () => {
    const uuid = 'a1b2c3d4-0000-4000-8000-000000000000'
    mockAuditApi(1, () => [
      event({ target_type: 'job', target_id: uuid, target_job_id: undefined }),
    ])
    renderPage()

    expect(await screen.findByRole('link', { name: uuid })).toHaveAttribute(
      'href',
      `/results/${uuid}`,
    )
  })

  it('shows a job target with no label as plain text and no dead link', async () => {
    mockAuditApi(1, () => [
      event({ target_type: 'job', target_id: 'job-gone', target_label: null }),
    ])
    renderPage()

    expect(await screen.findByText('job-gone')).toBeInTheDocument()
    expect(screen.queryByRole('link')).not.toBeInTheDocument()
  })

  it('shows a job target with no resolved job id as plain text and no dead link', async () => {
    mockAuditApi(1, () => [
      // A uuid-shaped id does not override an explicit null: the job is gone.
      event({
        target_type: 'job',
        target_id: 'a1b2c3d4-0000-4000-8000-000000000000',
        target_job_id: null,
        target_label: null,
      }),
    ])
    renderPage()

    expect(await screen.findByText('a1b2c3d4-0000-4000-8000-000000000000')).toBeInTheDocument()
    expect(screen.queryByRole('link')).not.toBeInTheDocument()
  })

  it('says which audit event deleted a gone job and links to it', async () => {
    mockAuditApi(1, () => [
      event({
        target_type: 'job',
        target_id: 'job-42',
        target_job_id: null,
        target_label: 'e2e-gcp-ovn-42',
        target_deleted_by: 'alice',
        target_deleted_at: '2024-03-04T05:06:07Z',
        target_delete_event_id: 42,
      }),
    ])
    renderPage()

    expect(await screen.findByText('e2e-gcp-ovn-42')).toBeInTheDocument()
    const evidence = screen.getByRole('link', { name: 'deleted by alice' })
    expect(evidence).toHaveAttribute('href', '/admin/audit?event=42')
    await userEvent.hover(evidence)
    const tooltip = (await screen.findByRole('tooltip')).textContent
    expect(tooltip).toContain(formatTimestamp('2024-03-04T05:06:07Z'))
    expect(tooltip).toContain('open the deletion event')
  })

  it('never claims a deletion it has no evidence for', async () => {
    mockAuditApi(1, () => [
      event({
        target_type: 'job',
        target_id: 'job-42',
        target_job_id: null,
        target_label: 'e2e-gcp-ovn-42',
        target_deleted_by: null,
        target_deleted_at: null,
        target_delete_event_id: null,
      }),
    ])
    renderPage()

    expect(await screen.findByText('e2e-gcp-ovn-42')).toBeInTheDocument()
    await userEvent.hover(screen.getByText('not found'))
    expect((await screen.findByRole('tooltip')).textContent).toContain('predate the audit trail')
    // No evidence, no link: the marker is the whole story.
    expect(screen.queryByRole('link')).not.toBeInTheDocument()
  })

  it('leaves a live job as a plain report link with no marker', async () => {
    mockAuditApi(1, () => [
      event({
        target_type: 'job',
        target_id: 'job-42',
        target_job_id: 'job-42',
        target_label: 'e2e-gcp-ovn-42',
        target_deleted_by: null,
        target_deleted_at: null,
        target_delete_event_id: null,
      }),
    ])
    renderPage()

    expect(await screen.findByRole('link', { name: 'e2e-gcp-ovn-42' })).toHaveAttribute(
      'href',
      '/results/job-42',
    )
    expect(screen.queryByText('not found')).not.toBeInTheDocument()
    expect(screen.queryByText(/^deleted by /)).not.toBeInTheDocument()
  })

  it('pins the row named by ?event= and drops the pin on Clear', async () => {
    mockAuditApi(1, () => [
      event({
        id: 42,
        target_type: 'job',
        target_id: 'job-42',
        target_job_id: null,
        target_label: 'e2e-gcp-ovn-42',
        target_deleted_by: 'alice',
        target_deleted_at: '2024-03-04T05:06:07Z',
        target_delete_event_id: 42,
      }),
    ])
    renderPage('/admin/audit?event=42&actor=bob')

    // Only the id is requested: an active filter must not hide the evidence.
    await waitFor(() =>
      expect(apiGet).toHaveBeenCalledWith('/api/admin/audit?limit=50&offset=0&id=42'),
    )
    expect(await screen.findByText('e2e-gcp-ovn-42')).toBeInTheDocument()
    expect(screen.getByTestId('location')).toHaveTextContent('event=42')

    fireEvent.click(screen.getByText('Clear'))
    await waitFor(() => expect(screen.getByTestId('location')).not.toHaveTextContent('event='))
    await waitFor(() => expect(apiGet).toHaveBeenLastCalledWith('/api/admin/audit?limit=50&offset=0'))
  })

  it('links a comment target to the report anchor of its job', async () => {
    mockAuditApi(1, () => [
      event({
        target_type: 'comment',
        target_id: '7',
        target_job_id: 'job-42',
        target_label: 'e2e-gcp-ovn-42',
        details: { job_id: 'job-42' },
      }),
    ])
    renderPage()

    const link = await screen.findByRole('link', { name: 'comment on e2e-gcp-ovn-42' })
    expect(link).toHaveAttribute('href', '/results/job-42#comment-7')
    await userEvent.hover(link)
    expect((await screen.findByRole('tooltip')).textContent).toBe('comment:7')
  })

  it('takes the comment anchor from the resolved job id, not from details', async () => {
    mockAuditApi(1, () => [
      event({
        target_type: 'comment',
        target_id: '7',
        target_job_id: 'uuid-1',
        target_label: 'e2e-gcp-ovn-42',
      }),
    ])
    renderPage()

    const link = await screen.findByRole('link', { name: 'comment on e2e-gcp-ovn-42' })
    expect(link).toHaveAttribute('href', '/results/uuid-1#comment-7')
  })

  it('keeps a comment with no resolved job id as plain text', async () => {
    mockAuditApi(1, () => [
      event({ target_type: 'comment', target_id: '7', target_job_id: null, target_label: null }),
    ])
    renderPage()

    expect(await screen.findByText('comment on 7')).toBeInTheDocument()
    expect(screen.queryByRole('link')).not.toBeInTheDocument()
  })

  it.each([
    ['user', 'alice', '/admin/users?user=alice'],
    ['provider', 'claude', '/admin/settings'],
    ['setting', 'ai_call_timeout', '/admin/settings?highlight=ai_call_timeout'],
    ['transcript', 'sess-1', '/admin/transcripts?session=sess-1'],
  ])('links a %s target to its page with the id as text', async (type, id, href) => {
    mockAuditApi(1, () => [event({ target_type: type, target_id: id, target_label: null })])
    renderPage()

    const link = await screen.findByRole('link', { name: id })
    expect(link).toHaveAttribute('href', href)
  })

  it('links a settings update that names its keys in details, not in target_id', async () => {
    mockAuditApi(1, () => [
      event({ target_type: 'setting', target_id: '', details: { changed_keys: ['ai_model'] } }),
    ])
    renderPage()

    expect(await screen.findByRole('link', { name: 'ai_model' })).toHaveAttribute(
      'href',
      '/admin/settings?highlight=ai_model',
    )
  })

  it('links the first changed key and lists the rest as plain text', async () => {
    mockAuditApi(1, () => [
      event({
        target_type: 'setting',
        target_id: '',
        details: { changed_keys: ['ai_model', 'ai_provider', 'ai_call_timeout'] },
      }),
    ])
    renderPage()

    const link = await screen.findByRole('link', { name: 'ai_model' })
    expect(link).toHaveAttribute('href', '/admin/settings?highlight=ai_model')
    // One destination, not three: only the first key is a link.
    expect(screen.getAllByRole('link')).toHaveLength(1)
    expect(link.closest('td')).toHaveTextContent('ai_model ai_provider, ai_call_timeout')
  })

  it('links a settings action with no target id through its first changed key', async () => {
    mockAuditApi(1, () => [
      event({
        action: 'admin.setting_reset',
        target_type: null,
        target_id: null,
        details: { changed_keys: ['jenkins_url'] },
      }),
    ])
    renderPage()

    expect(await screen.findByRole('link', { name: 'jenkins_url' })).toHaveAttribute(
      'href',
      '/admin/settings?highlight=jenkins_url',
    )
  })

  it('links a failure target to the report of the job it belongs to', async () => {
    const failure = '8f14e45f-ceea-467a-9575-9d1c0c1b0d0f'
    mockAuditApi(1, () => [
      event({ target_type: 'failure', target_id: failure, details: { job_id: 'job-9' } }),
    ])
    renderPage()

    expect(await screen.findByRole('link', { name: failure })).toHaveAttribute(
      'href',
      '/results/job-9',
    )
  })

  it('links a feedback target to its tracker issue, named by the issue number', async () => {
    mockAuditApi(1, () => [
      event({
        target_type: 'feedback',
        target_id: '42',
        target_label: null,
        details: {
          issue_number: 382,
          issue_url: 'https://github.com/myk-org/rootcoz/issues/382',
        },
      }),
    ])
    renderPage()

    const link = await screen.findByRole('link', { name: 'issue #382' })
    expect(link).toHaveAttribute('href', 'https://github.com/myk-org/rootcoz/issues/382')
    // Somebody else's site: it leaves this tab and never learns where it came from.
    expect(link).toHaveAttribute('rel', 'noreferrer')
  })

  it('keeps a feedback target with no issue url as plain text', async () => {
    mockAuditApi(1, () => [
      event({ target_type: 'feedback', target_id: '42', details: { issue_number: 382 } }),
    ])
    renderPage()

    expect(await screen.findByText('issue #382')).toBeInTheDocument()
    expect(screen.queryByRole('link')).not.toBeInTheDocument()
  })

  it('names the actor who deleted a gone user and links that evidence', async () => {
    mockAuditApi(1, () => [
      event({
        actor: 'carol',
        target_type: 'user',
        target_id: 'alice',
        target_label: null,
        target_gone: true,
        target_deleted_by: 'bob',
        target_deleted_at: '2024-03-04T05:06:07Z',
        target_delete_event_id: 7,
      }),
    ])
    renderPage()

    // The name stays put; the marker is a second line under it.
    expect(await screen.findByText('alice')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'deleted by bob' })).toHaveAttribute(
      'href',
      '/admin/audit?event=7',
    )
    expect(screen.queryByRole('link', { name: 'alice' })).not.toBeInTheDocument()
  })

  it('says a gone provider is not found when no removal was recorded', async () => {
    mockAuditApi(1, () => [
      event({
        target_type: 'provider',
        target_id: 'claude',
        target_label: null,
        target_gone: true,
        target_deleted_by: null,
        target_deleted_at: null,
        target_delete_event_id: null,
      }),
    ])
    renderPage()

    expect(await screen.findByText('claude')).toBeInTheDocument()
    await userEvent.hover(screen.getByText('not found'))
    expect((await screen.findByRole('tooltip')).textContent).toContain(
      'No destination and no removal recorded',
    )
    expect(screen.queryByRole('link')).not.toBeInTheDocument()
  })

  it('blames retention, not a person, for a gone transcript', async () => {
    mockAuditApi(1, () => [
      event({
        target_type: 'transcript',
        target_id: 'sess-1',
        target_label: null,
        target_gone: true,
        target_deleted_by: null,
        target_deleted_at: null,
        target_delete_event_id: null,
      }),
    ])
    renderPage()

    expect(await screen.findByText('sess-1')).toBeInTheDocument()
    await userEvent.hover(screen.getByText('not found'))
    expect((await screen.findByRole('tooltip')).textContent).toContain(
      "Transcripts are removed by the sidecar's retention",
    )
    expect(screen.queryByText(/^deleted by /)).not.toBeInTheDocument()
    expect(screen.queryByRole('link')).not.toBeInTheDocument()
  })

  it('names the actor who deleted a gone setting and drops its link', async () => {
    mockAuditApi(1, () => [
      event({
        target_type: 'setting',
        target_id: 'enable_auto_review',
        target_label: null,
        target_gone: true,
        target_deleted_by: 'bob',
        target_deleted_at: '2024-03-04T05:06:07Z',
        target_delete_event_id: 9,
      }),
    ])
    renderPage()

    expect(await screen.findByText('enable_auto_review')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'deleted by bob' })).toHaveAttribute(
      'href',
      '/admin/audit?event=9',
    )
    expect(screen.queryByRole('link', { name: 'enable_auto_review' })).not.toBeInTheDocument()
  })

  it.each(['user', 'provider', 'setting'])(
    'does not mark a live %s target gone while the read model has not answered',
    async (type) => {
      // A streamed event carries no `target_gone` at all: absence is not proof.
      mockAuditApi(1, () => [
        event({ target_type: type, target_id: 'target-1', target_label: null, target_gone: undefined }),
      ])
      renderPage()

      expect(await screen.findByText('target-1')).toBeInTheDocument()
      expect(screen.queryByText('not found')).not.toBeInTheDocument()
      expect(screen.queryByText(/^deleted by /)).not.toBeInTheDocument()
    },
  )

  it('keeps an unknown target type as plain type:id text', async () => {
    mockAuditApi(1, () => [
      event({ target_type: 'webhook', target_id: '9', target_label: null }),
    ])
    renderPage()

    expect(await screen.findByText('webhook:9')).toBeInTheDocument()
    expect(screen.queryByRole('link')).not.toBeInTheDocument()
  })

  it('sets the actor filter from a click on the actor name', async () => {
    mockAuditApi(1, () => [event({ actor: 'bob' })])
    renderPage()

    fireEvent.click(await screen.findByRole('button', { name: 'bob' }))

    expect(screen.getByLabelText('Actor filter')).toHaveValue('bob')
    await waitFor(() =>
      expect(apiGet).toHaveBeenLastCalledWith('/api/admin/audit?limit=50&offset=0&actor=bob'),
    )
    // The click filters; it must not also count as a click on the row.
    expect(screen.queryByTestId('event-details')).toBeNull()
    expect(screen.getByText('Settings updated').closest('tr')).toHaveAttribute(
      'aria-expanded',
      'false',
    )
  })

  it('links the id-valued details keys to the report they name', async () => {
    mockAuditApi(1, () => [
      event({
        target_type: 'job',
        target_id: 'job-42',
        target_label: 'e2e-gcp-ovn-42',
        details: { job_id: 'job-42', comment_id: '7', failure_uuid: 'uuid-1', note: 'plain' },
      }),
    ])
    renderPage()

    fireEvent.click(await screen.findByText('Settings updated'))
    const details = await screen.findByTestId('event-details')

    const comment = within(details).getByRole('link', { name: '7' })
    expect(comment).toHaveAttribute('href', '/results/job-42#comment-7')
    expect(within(details).getByRole('link', { name: 'uuid-1' })).toHaveAttribute(
      'href',
      '/results/job-42',
    )
    expect(within(details).getByRole('link', { name: 'job-42' })).toHaveAttribute(
      'href',
      '/results/job-42',
    )
    // A value with no destination stays plain text.
    expect(within(details).getByText('plain')).toBeInTheDocument()
  })

  it('shows a load error using the API detail', async () => {
    apiGet.mockImplementation((path: string) =>
      path.startsWith('/api/admin/audit/actions')
        ? Promise.resolve({ actions: [] })
        : Promise.reject(new Error('500: audit table unavailable')),
    )
    const { container } = renderPage()

    expect(await screen.findByText('500: audit table unavailable')).toBeInTheDocument()
    // A 401-only affordance: nothing to sign in to for a plain server error.
    expect(screen.queryByRole('button', { name: 'Sign in' })).not.toBeInTheDocument()
    // A failed request is not an outstanding one: no skeleton, no "empty" claim.
    expect(container.querySelector('.animate-pulse')).toBeNull()
    expect(screen.queryByText('No audit events yet')).not.toBeInTheDocument()
  })

  it('offers a re-login path when the list request is rejected with 401', async () => {
    apiGet.mockImplementation((path: string) =>
      path.startsWith('/api/admin/audit/actions')
        ? Promise.resolve({ actions: [] })
        : Promise.reject(new ApiError(401, 'Unauthorized', { detail: 'Session expired' })),
    )
    renderPage()

    expect(await screen.findByText(/session expired — sign in again/i)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Sign in' })).toBeInTheDocument()
  })

  it('ends the session before showing the login form on a 401', async () => {
    apiGet.mockImplementation((path: string) =>
      path.startsWith('/api/admin/audit/actions')
        ? Promise.resolve({ actions: [] })
        : Promise.reject(new ApiError(401, 'Unauthorized', { detail: 'Session expired' })),
    )
    const { router } = renderRouted(['/admin/audit'])

    fireEvent.click(await screen.findByRole('button', { name: 'Sign in' }))

    // Logging out first is what keeps RegisterPage from bouncing to the dashboard.
    await waitFor(() => expect(router.state.location.pathname).toBe('/login'))
    expect(logout).toHaveBeenCalledTimes(1)
  })

  it('still clears the session and reaches the login form when logout rejects', async () => {
    logout.mockRejectedValueOnce(new Error('session already gone'))
    setUsername('alice')
    setGithubToken('gh-token')
    apiGet.mockImplementation((path: string) =>
      path.startsWith('/api/admin/audit/actions')
        ? Promise.resolve({ actions: [] })
        : Promise.reject(new ApiError(401, 'Unauthorized', { detail: 'Session expired' })),
    )
    const { router } = renderRouted(['/admin/audit'])

    fireEvent.click(await screen.findByRole('button', { name: 'Sign in' }))

    await waitFor(() => expect(router.state.location.pathname).toBe('/login'))
    expect(logout).toHaveBeenCalledTimes(1)
    expect(getUsername()).toBe('')
    expect(getGithubToken()).toBe('')
  })

  it('shows the plain error and no sign-in action for a 500', async () => {
    apiGet.mockImplementation((path: string) =>
      path.startsWith('/api/admin/audit/actions')
        ? Promise.resolve({ actions: [] })
        : Promise.reject(new ApiError(500, 'Internal Server Error', { detail: 'audit table unavailable' })),
    )
    renderPage()

    expect(await screen.findByText('audit table unavailable')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Sign in' })).not.toBeInTheDocument()
    expect(screen.queryByText(/sign in again/i)).not.toBeInTheDocument()
  })

  it('labels the time column as UTC with the explanation in its tooltip', async () => {
    mockAuditApi(1)
    renderPage()
    await screen.findByText('alice')

    const marker = screen.getByTestId('time-basis')
    expect(marker).toHaveTextContent('Time UTC')
    // The explanation is on the marker itself, not on every row.
    expect(marker.getAttribute('aria-label')).toContain('date filters are UTC')
    await userEvent.hover(marker)
    expect((await screen.findByRole('tooltip')).textContent).toContain('date filters are UTC')
  })

  it('restores filters from the URL and mirrors filter edits back into it', async () => {
    mockAuditApi(1)
    renderPage('/admin/audit?actor=alice&outcome=failure')

    // The shared link's filters reach the first request and the inputs.
    await waitFor(() =>
      expect(apiGet).toHaveBeenCalledWith(
        '/api/admin/audit?limit=50&offset=0&actor=alice&outcome=failure',
      ),
    )
    expect(screen.getByLabelText('Actor filter')).toHaveValue('alice')
    expect(screen.getByRole('combobox', { name: 'Outcome filter' })).toHaveTextContent('failure')

    fireEvent.change(screen.getByLabelText('Since date'), { target: { value: '2024-01-01' } })
    await waitFor(() =>
      expect(screen.getByTestId('location')).toHaveTextContent('since=2024-01-01'),
    )
    expect(screen.getByTestId('location')).toHaveTextContent('actor=alice')
    expect(screen.getByTestId('location')).toHaveTextContent('outcome=failure')

    // Clearing drops the filter params instead of storing empty values.
    fireEvent.click(screen.getByText('Clear'))
    await waitFor(() => expect(screen.getByTestId('location')).not.toHaveTextContent('actor='))
    expect(screen.getByTestId('location')).not.toHaveTextContent('outcome=')
    expect(screen.getByTestId('location')).not.toHaveTextContent('since=')
  })

  it('adopts a URL the browser navigates back to while the page stays mounted', async () => {
    mockAuditApi(120, pageOfEvents(120))
    // An earlier entry with other filters and a shared link on top of it.
    const { router } = renderRouted(
      ['/admin/audit?actor=bob&outcome=failure&limit=100&live=1', '/admin/audit?actor=alice'],
      1,
    )

    expect(await screen.findByText('Showing 1–50 of 120')).toBeInTheDocument()
    expect(screen.getByLabelText('Actor filter')).toHaveValue('alice')
    await waitFor(() =>
      expect(apiGet).toHaveBeenLastCalledWith('/api/admin/audit?limit=50&offset=0&actor=alice'),
    )

    // Back: the component is not remounted, only the query changes.
    await act(async () => {
      await router.navigate(-1)
    })

    expect(screen.getByLabelText('Actor filter')).toHaveValue('bob')
    expect(screen.getByRole('combobox', { name: 'Outcome filter' })).toHaveTextContent('failure')
    expect(screen.getByRole('combobox', { name: 'Rows per page' })).toHaveTextContent('100')
    expect(liveSwitch()).toHaveAttribute('aria-checked', 'true')
    // The next request asks for what the URL says, from the first page.
    await waitFor(() =>
      expect(apiGet).toHaveBeenLastCalledWith(
        '/api/admin/audit?limit=100&offset=0&actor=bob&outcome=failure',
      ),
    )
    // Adopting the URL rewrites nothing: no pushed entry and no replace either.
    expect(router.state.historyAction).toBe('POP')
    expect(locationKeys).toHaveLength(2)
  })

  it('wraps an over-long actor instead of widening the table', async () => {
    // An over-long username attempt really did reach the audit log at 257 chars.
    const actor = 'z'.repeat(300)
    mockAuditApi(1, () => [event({ actor })])
    const { container } = renderPage()

    const name = await screen.findByText(actor)
    const cell = name.closest('td')!

    // The long token breaks; the cell is bounded and top-aligned.
    expect(cell).toHaveClass('break-all', 'align-top', 'max-w-[22ch]')
    expect(cell).toHaveTextContent(actor)

    // A fixed layout plus a width on every header is what stops one cell from
    // pushing the other columns out of view.
    const table = container.querySelector('table')!
    expect(table).toHaveClass('table-fixed')
    for (const head of screen.getAllByRole('columnheader')) {
      expect(head.className).toMatch(/w-\[9rem\]|w-\[22ch\]|w-\[26ch\]|w-\[8rem\]/)
    }

    // No cell renders unbreakable text: each one wraps or is a single-line narrow
    // value (time, outcome).
    for (const td of screen.getAllByRole('cell')) {
      expect(td.className).toMatch(/align-top/)
      expect(td.className).toMatch(/break-all|break-words|whitespace-nowrap/)
    }
  })

  it('shows the empty state', async () => {
    mockAuditApi(0, () => [])
    renderPage()

    expect(await screen.findByText('No audit events yet')).toBeInTheDocument()
    expect(screen.getByText('Showing 0–0 of 0')).toBeInTheDocument()
  })

  it('expands a row to show the details and request_ip', async () => {
    mockAuditApi(1)
    renderPage()

    // The action label, not the actor: the actor name now sets the filter.
    fireEvent.click(await screen.findByText('Settings updated'))

    const details = await screen.findByTestId('event-details')
    expect(details).toHaveTextContent('ai_call_timeout')
    expect(details).toHaveTextContent('changed')
    expect(screen.getByText('request_ip: 10.0.0.7')).toBeInTheDocument()
  })

  it('debounces text filters and resets the offset', async () => {
    vi.useFakeTimers()
    mockAuditApi(120, (offset) => [event({ id: offset + 1 })])
    renderPage()

    await act(async () => {
      await vi.advanceTimersByTimeAsync(0)
    })
    // Move to page 2 first so the filter change has an offset to reset.
    fireEvent.click(screen.getByText('Next'))
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0)
    })
    expect(apiGet).toHaveBeenLastCalledWith('/api/admin/audit?limit=50&offset=50')

    apiGet.mockClear()
    fireEvent.change(screen.getByLabelText('Actor filter'), { target: { value: 'alice' } })

    await act(async () => {
      await vi.advanceTimersByTimeAsync(299)
    })
    expect(apiGet).not.toHaveBeenCalled()

    await act(async () => {
      await vi.advanceTimersByTimeAsync(1)
    })
    expect(apiGet).toHaveBeenCalledWith('/api/admin/audit?limit=50&offset=0&actor=alice')
  })

  it('pages with prev/next and reports the visible range', async () => {
    mockAuditApi(120, (offset) => [event({ id: offset + 1 })])
    renderPage()

    expect(await screen.findByText('Showing 1–1 of 120')).toBeInTheDocument()

    fireEvent.click(screen.getByText('Next'))
    await waitFor(() =>
      expect(apiGet).toHaveBeenLastCalledWith('/api/admin/audit?limit=50&offset=50'),
    )
    expect(await screen.findByText('Showing 51–51 of 120')).toBeInTheDocument()

    fireEvent.click(screen.getByText('Previous'))
    await waitFor(() =>
      expect(apiGet).toHaveBeenLastCalledWith('/api/admin/audit?limit=50&offset=0'),
    )
    expect(await screen.findByText('Showing 1–1 of 120')).toBeInTheDocument()
  })

  it('sends the default page size of 50 and shows it in the rows-per-page control', async () => {
    mockAuditApi(1)
    renderPage()

    await screen.findByText('alice')
    expect(apiGet).toHaveBeenCalledWith('/api/admin/audit?limit=50&offset=0')
    expect(screen.getByRole('combobox', { name: 'Rows per page' })).toHaveTextContent('50')
  })

  it('changes the page size to 100, resets the offset and persists ?limit=100', async () => {
    mockAuditApi(120, pageOfEvents(120))
    renderPage('/admin/audit?q=alice')

    expect(await screen.findByText('Showing 1–50 of 120')).toBeInTheDocument()
    fireEvent.click(screen.getByText('Next'))
    await waitFor(() =>
      expect(apiGet).toHaveBeenLastCalledWith('/api/admin/audit?limit=50&offset=50&q=alice'),
    )

    const user = userEvent.setup()
    await user.click(screen.getByRole('combobox', { name: 'Rows per page' }))
    await user.click(screen.getByRole('option', { name: '100' }))

    // Offset back to the first page, refetched with the new page size.
    await waitFor(() =>
      expect(apiGet).toHaveBeenLastCalledWith('/api/admin/audit?limit=100&offset=0&q=alice'),
    )
    expect(screen.getByTestId('location')).toHaveTextContent('/admin/audit?q=alice&limit=100')
    expect(await screen.findByText('Showing 1–100 of 120')).toBeInTheDocument()
  })

  it('persists the offset in the URL with a replace, so a reload shows that page', async () => {
    mockAuditApi(120, pageOfEvents(120))
    const { router } = renderRouted(['/admin/audit'])

    expect(await screen.findByText('Showing 1–50 of 120')).toBeInTheDocument()

    fireEvent.click(screen.getByText('Next'))
    await waitFor(() =>
      expect(apiGet).toHaveBeenLastCalledWith('/api/admin/audit?limit=50&offset=50'),
    )
    expect(screen.getByTestId('location')).toHaveTextContent('/admin/audit?offset=50')
    // replace, not push: paging does not fill the Back stack with pages.
    expect(router.state.historyAction).toBe('REPLACE')
    expect(locationKeys).toHaveLength(2)

    fireEvent.click(screen.getByText('Previous'))
    await waitFor(() =>
      expect(apiGet).toHaveBeenLastCalledWith('/api/admin/audit?limit=50&offset=0'),
    )
    // The first page is the URL with no offset at all.
    expect(screen.getByTestId('location')).toHaveTextContent(/\/admin\/audit$/)
  })

  it('honours ?offset=50&limit=50 on mount and reports the same range', async () => {
    mockAuditApi(120, pageOfEvents(120))
    renderPage('/admin/audit?offset=50&limit=50')

    await waitFor(() =>
      expect(apiGet).toHaveBeenLastCalledWith('/api/admin/audit?limit=50&offset=50'),
    )
    expect(await screen.findByText('Showing 51–100 of 120')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Previous' })).toBeEnabled()
  })

  it('follows a shared link whose window is on a later page of a filtered set', async () => {
    mockAuditApi(120, pageOfEvents(120))
    renderPage('/admin/audit?q=alice&offset=100&limit=25')

    await waitFor(() =>
      expect(apiGet).toHaveBeenLastCalledWith('/api/admin/audit?limit=25&offset=100&q=alice'),
    )
    expect(await screen.findByText('Showing 101–120 of 120')).toBeInTheDocument()
  })

  it('honours ?limit=250 on mount and ignores an invalid ?limit=9999', async () => {
    mockAuditApi(120, pageOfEvents(120))
    const { unmount } = renderPage('/admin/audit?limit=250')

    await waitFor(() => expect(apiGet).toHaveBeenCalledWith('/api/admin/audit?limit=250&offset=0'))
    expect(screen.getByRole('combobox', { name: 'Rows per page' })).toHaveTextContent('250')
    unmount()

    apiGet.mockClear()
    renderPage('/admin/audit?limit=9999')

    await waitFor(() => expect(apiGet).toHaveBeenCalledWith('/api/admin/audit?limit=50&offset=0'))
    expect(screen.getByRole('combobox', { name: 'Rows per page' })).toHaveTextContent('50')
  })

  it('subscribes the live tail, prepends, and closes on toggle-off and unmount', async () => {
    mockAuditApi(1)
    const { unmount } = renderPage()
    await screen.findByText('alice')
    expect(MockEventSource.instances).toHaveLength(0)

    const { liveSwitch, source } = enableLive()
    expect(MockEventSource.instances).toHaveLength(1)
    expect(source.url).toBe('/api/admin/audit/stream')
    act(() => {
      source.emit('audit:event', event({ id: 2, actor: 'bob', action: 'user.role.change' }))
    })

    expect(await screen.findByText('bob')).toBeInTheDocument()
    expect(screen.getByText('Role changed')).toBeInTheDocument()
    expect(screen.getByText('Showing 1–2 of 2')).toBeInTheDocument()

    fireEvent.click(liveSwitch)
    expect(source.closed).toBe(true)

    fireEvent.click(liveSwitch)
    const second = MockEventSource.instances[1]
    unmount()
    expect(second.closed).toBe(true)
  })

  it('clears the error banner when the same query succeeds after a failure', async () => {
    let failed = false
    apiGet.mockImplementation((path: string) => {
      if (path.startsWith('/api/admin/audit/actions')) return Promise.resolve({ actions: ACTIONS })
      if (path.includes('actor=alice')) {
        return Promise.resolve({
          events: [event({ id: 2, actor: 'bob', action: 'user.role.change' })],
          total: 1,
          limit: 50,
          offset: 0,
        })
      }
      if (!failed) {
        failed = true
        return Promise.reject(new Error('500: audit table unavailable'))
      }
      return Promise.resolve({ events: [event()], total: 1, limit: 50, offset: 0 })
    })
    renderPage()

    expect(await screen.findByText('500: audit table unavailable')).toBeInTheDocument()

    await setActor('alice')
    expect(await screen.findByText('Role changed')).toBeInTheDocument()

    // Back to the query that failed before: its banner must not come back.
    await setActor('')
    expect(await screen.findByText('Settings updated')).toBeInTheDocument()
    expect(screen.queryByText('500: audit table unavailable')).not.toBeInTheDocument()
  })

  it('ignores a streamed event that does not match the active filters', async () => {
    mockAuditApi(1)
    renderPage()
    await screen.findByText('alice')

    await setActor('bob')
    const { source } = enableLive()

    act(() => {
      source.emit('audit:event', event({ id: 9, action: 'user.role.change' }))
    })

    expect(screen.queryByText('Role changed')).not.toBeInTheDocument()
    expect(screen.getAllByRole('row')).toHaveLength(2)
    expect(screen.getByText('Showing 1–1 of 1')).toBeInTheDocument()
  })

  it('counts but does not insert a matching streamed event on a later page', async () => {
    mockAuditApi(120, (offset) => [event({ id: offset + 1 })])
    renderPage()
    expect(await screen.findByText('Showing 1–1 of 120')).toBeInTheDocument()

    fireEvent.click(screen.getByText('Next'))
    await waitFor(() =>
      expect(apiGet).toHaveBeenLastCalledWith('/api/admin/audit?limit=50&offset=50'),
    )
    expect(await screen.findByText('Showing 51–51 of 120')).toBeInTheDocument()

    const { source } = enableLive()
    act(() => {
      source.emit('audit:event', event({ id: 5000, actor: 'bob', action: 'user.role.change' }))
    })

    expect(screen.queryByText('Role changed')).not.toBeInTheDocument()
    expect(screen.getAllByRole('row')).toHaveLength(2)
    expect(screen.getByText('Showing 51–51 of 121')).toBeInTheDocument()
  })

  it('restores the tail from ?live=1 and starts out connecting', async () => {
    mockAuditApi(1)
    renderPage('/admin/audit?live=1')

    expect(liveSwitch()).toHaveAttribute('aria-checked', 'true')
    expect(MockEventSource.instances).toHaveLength(1)
    expect(MockEventSource.instances[0].url).toBe('/api/admin/audit/stream')
    expect(liveDot()).toHaveClass('bg-signal-amber')
    expect(liveSwitch()).toHaveAttribute('aria-label', expect.stringContaining('Connecting'))
    expect(await liveTooltip()).toContain('Connecting to the live audit stream')
  })

  it('turns green when the stream opens', async () => {
    mockAuditApi(1)
    renderPage()
    const { source } = enableLive()

    act(() => {
      source.onopen?.()
    })

    expect(liveDot()).toHaveClass('animate-pulse', 'bg-signal-green')
    expect(liveSwitch()).toHaveAttribute('aria-label', expect.stringContaining('Receiving live audit events'))
    expect(await liveTooltip()).toContain('Receiving live audit events')
  })

  it('switches the tail off on error and leaves the failure visible without retrying', async () => {
    mockAuditApi(1)
    renderPage('/admin/audit?q=alice&live=1')
    const source = MockEventSource.instances[0]

    act(() => {
      source.onerror?.()
    })

    expect(source.closed).toBe(true)
    // Switch off, ?live=1 gone, other params kept — the admin re-enables it.
    expect(liveSwitch()).toHaveAttribute('aria-checked', 'false')
    expect(screen.getByTestId('location')).toHaveTextContent('/admin/audit?q=alice')
    expect(liveDot()).toHaveClass('bg-signal-red')
    expect(liveSwitch()).toHaveAttribute('aria-label', expect.stringContaining('Live stream failed'))
    expect(await liveTooltip()).toContain('switch Live on to try again')
    // Dot + label + tooltip are the only signal: nothing inline beside the switch.
    expect(screen.getByTestId('live-status').textContent).toBe('')
    // No automatic reconnect.
    expect(MockEventSource.instances).toHaveLength(1)
  })

  it('creates exactly one new stream when the admin enables it after a failure', async () => {
    mockAuditApi(1)
    renderPage('/admin/audit?live=1')

    act(() => {
      MockEventSource.instances[0].onerror?.()
    })
    expect(MockEventSource.instances).toHaveLength(1)

    fireEvent.click(liveSwitch())

    expect(MockEventSource.instances).toHaveLength(2)
    expect(MockEventSource.instances[1].url).toBe('/api/admin/audit/stream')
    expect(liveSwitch()).toHaveAttribute('aria-checked', 'true')
    expect(liveSwitch()).toHaveAttribute('aria-label', expect.stringContaining('Connecting'))
    expect(liveDot()).toHaveClass('bg-signal-amber')
    expect(screen.getByTestId('location')).toHaveTextContent('/admin/audit?live=1')
  })

  it('writes live=1 on and removes it off while preserving other params', async () => {
    mockAuditApi(1)
    renderPage('/admin/audit?q=alice')

    fireEvent.click(liveSwitch())
    expect(screen.getByTestId('location')).toHaveTextContent('/admin/audit?q=alice&live=1')

    fireEvent.click(liveSwitch())
    expect(screen.getByTestId('location')).toHaveTextContent('/admin/audit?q=alice')
    expect(screen.getByTestId('location')).not.toHaveTextContent('live=1')
  })

  it('does not offer expansion for a row with no details', async () => {
    mockAuditApi(1, () => [event({ details: {} })])
    renderPage()

    const row = (await screen.findByText('No details recorded')).closest('tr')
    expect(row).toBeTruthy()
    expect(row!).not.toHaveAttribute('aria-expanded')
    expect(row!).not.toHaveClass('cursor-pointer')
  })

  it('renders label, dot and switch tightly together and greys the dot when off', async () => {
    mockAuditApi(1)
    renderPage()
    const { source } = enableLive()

    // No text label: dot + tooltip carry the state, so nothing can shift.
    expect(screen.queryByTestId('live-label')).toBeNull()
    expect(liveDot().nextElementSibling).toHaveAttribute('role', 'switch')

    act(() => {
      source.onopen?.()
    })
    expect(liveSwitch()).toHaveAttribute('aria-label', expect.stringContaining('Receiving live audit events'))

    act(() => {
      source.onerror?.()
    })
    expect(liveSwitch()).toHaveAttribute('aria-label', expect.stringContaining('Live stream failed'))

    fireEvent.click(liveSwitch())
    fireEvent.click(liveSwitch())
    expect(liveSwitch()).toHaveAttribute('aria-label', expect.stringContaining('Live tail is off'))
    expect(liveDot()).toHaveClass('bg-border-default')
    expect(await liveTooltip()).toContain('Live tail is off')
  })

  it('shows how many events the stream dropped when audit-gap arrives', async () => {
    mockAuditApi(1)
    renderPage()
    await screen.findByText('alice')

    const { source } = enableLive()
    act(() => {
      source.emit('audit-gap', { dropped: 3 })
    })

    expect(await screen.findByText(/Missed 3 live events/)).toBeInTheDocument()
    // The stream is still healthy — nothing else about the page changes.
    expect(screen.getByText('alice')).toBeInTheDocument()
    expect(liveSwitch()).toHaveAttribute('aria-checked', 'true')
  })

  it('clears the gap notice and refetches the current view when Refresh is clicked', async () => {
    mockAuditApi(1)
    renderPage('/admin/audit?q=alice&offset=0')
    await screen.findByText('alice')

    const { source } = enableLive()
    act(() => {
      source.emit('audit-gap', { dropped: 3 })
    })
    expect(await screen.findByText(/Missed 3 live events/)).toBeInTheDocument()

    apiGet.mockClear()
    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }))

    // Same filters, same offset: it refreshes the view instead of resetting it.
    await waitFor(() =>
      expect(apiGet).toHaveBeenCalledWith('/api/admin/audit?limit=50&offset=0&q=alice'),
    )
    await waitFor(() => expect(screen.queryByText(/Missed 3 live events/)).toBeNull())
  })

  it('shows no gap notice when only normal events arrive', async () => {
    mockAuditApi(1)
    renderPage()
    await screen.findByText('alice')

    const { source } = enableLive()
    act(() => {
      source.emit('audit:event', event({ id: 2, actor: 'bob', action: 'user.role.change' }))
    })

    expect(await screen.findByText('bob')).toBeInTheDocument()
    expect(screen.queryByText(/Missed \d+ live/)).toBeNull()
    expect(screen.queryByRole('button', { name: 'Refresh' })).toBeNull()
  })

  it('adds up the count across gap frames instead of showing only the last batch', async () => {
    mockAuditApi(1)
    renderPage()
    await screen.findByText('alice')

    const { source } = enableLive()
    act(() => {
      source.emit('audit-gap', { dropped: 3 })
    })
    expect(await screen.findByText(/Missed 3 live events/)).toBeInTheDocument()

    act(() => {
      source.emit('audit-gap', { dropped: 5 })
    })

    // Each frame reports only its own drops: 3 + 5 missed since the refresh.
    expect(await screen.findByText(/Missed 8 live events/)).toBeInTheDocument()
    expect(screen.queryByText(/Missed 3 live events/)).toBeNull()
    expect(screen.queryByText(/Missed 5 live events/)).toBeNull()
    expect(screen.getAllByRole('button', { name: 'Refresh' })).toHaveLength(1)
  })

  it('starts the gap count over after a refresh', async () => {
    mockAuditApi(1)
    renderPage()
    await screen.findByText('alice')

    const { source } = enableLive()
    act(() => {
      source.emit('audit-gap', { dropped: 3 })
    })
    expect(await screen.findByText(/Missed 3 live events/)).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }))
    await waitFor(() => expect(screen.queryByText(/Missed \d+ live/)).toBeNull())

    act(() => {
      source.emit('audit-gap', { dropped: 2 })
    })

    // The refreshed page is whole again: only the new gap counts.
    expect(await screen.findByText(/Missed 2 live events/)).toBeInTheDocument()
  })

  it('shows a single gap frame with its own count', async () => {
    mockAuditApi(1)
    renderPage()
    await screen.findByText('alice')

    const { source } = enableLive()
    act(() => {
      source.emit('audit-gap', { dropped: 1 })
    })

    expect(await screen.findByText(/Missed 1 live event since the last refresh/)).toBeInTheDocument()
  })

  it('promises exactly what the widened search matches', async () => {
    mockAuditApi(1)
    renderPage()

    expect(await screen.findByLabelText('Search filter')).toHaveAttribute(
      'placeholder',
      'Search actor, target, action or details…',
    )
  })

  it.each(['csv', 'json'] as const)(
    'exports the current filtered set as %s without any page bounds',
    async (format) => {
      mockAuditApi(7)
      renderPage(
        '/admin/audit?actor=alice&action=settings.update&outcome=failure&q=timeout&since=2024-01-01&until=2024-01-31',
      )
      await screen.findByText('alice')

      const hrefs: string[] = []
      const click = vi
        .spyOn(HTMLAnchorElement.prototype, 'click')
        .mockImplementation(function (this: HTMLAnchorElement) {
          hrefs.push(this.getAttribute('href') ?? '')
        })

      fireEvent.click(screen.getByRole('button', { name: 'Export filtered audit events' }))
      fireEvent.click(await screen.findByRole('button', { name: format.toUpperCase() }))
      click.mockRestore()

      // Every filter, both formats, and neither `limit` nor `offset`: the
      // export is the whole match, not the page on screen.
      expect(hrefs).toEqual([
        `/api/admin/audit/export?format=${format}&actor=alice&action=settings.update&outcome=failure&since=2024-01-01&until=2024-01-31&q=timeout`,
      ])
      expect(screen.getByTestId('export-status')).toHaveTextContent(
        `Exported 7 events as ${format.toUpperCase()}.`,
      )
    },
  )

  it('will not export a pinned row as if it were a set', async () => {
    mockAuditApi(1)
    renderPage('/admin/audit?event=42')
    await screen.findByText('alice')

    expect(screen.getByRole('button', { name: 'Export filtered audit events' })).toBeDisabled()
    expect(screen.getByTestId('export-status')).toHaveTextContent('clear the pin to export a set')
  })

  it.each([
    ['Last hour', 3_600_000],
    ['Last 24 hours', 86_400_000],
    ['Last 7 days', 604_800_000],
  ])('%s stamps a UTC since at click time and clears until', async (label, windowMs) => {
    mockAuditApi(1)
    renderPage('/admin/audit?until=2024-01-31')
    await screen.findByText('alice')

    fireEvent.click(screen.getByRole('button', { name: label }))
    await waitFor(() => expect(screen.getByTestId('location')).toHaveTextContent('since='))

    const params = new URLSearchParams(
      (screen.getByTestId('location').textContent ?? '').split('?')[1],
    )
    const since = params.get('since')
    expect(since).toBeTruthy()
    // The window is measured from the click, in UTC, and then fixed in the URL.
    expect(Math.abs(Date.parse(since!) - (Date.now() - windowMs))).toBeLessThan(5_000)
    expect(params.get('until')).toBeNull()
    expect(screen.getByRole('button', { name: label })).toHaveAttribute('aria-pressed', 'true')
    // The list asks for exactly the window the URL states.
    await waitFor(() =>
      expect(apiGet).toHaveBeenLastCalledWith(
        `/api/admin/audit?limit=50&offset=0&since=${encodeURIComponent(since!)}`,
      ),
    )
  })

  it('clears both bounds for All time', async () => {
    mockAuditApi(1)
    renderPage('/admin/audit?since=2024-01-01&until=2024-01-31')
    await screen.findByText('alice')

    fireEvent.click(screen.getByRole('button', { name: 'All time' }))

    await waitFor(() => expect(screen.getByTestId('location')).not.toHaveTextContent('since='))
    expect(screen.getByTestId('location')).not.toHaveTextContent('until=')
    await waitFor(() =>
      expect(apiGet).toHaveBeenLastCalledWith('/api/admin/audit?limit=50&offset=0'),
    )
    expect(screen.getByRole('button', { name: 'All time' })).toHaveAttribute('aria-pressed', 'true')
  })

  it('marks the preset a reloaded ?since= describes and keeps the exact bound visible', async () => {
    const since = presetSince(24)
    mockAuditApi(1)
    renderPage(`/admin/audit?since=${encodeURIComponent(since)}`)
    await screen.findByText('alice')

    expect(screen.getByRole('button', { name: 'Last 24 hours' })).toHaveAttribute(
      'aria-pressed',
      'true',
    )
    expect(screen.getByRole('button', { name: 'Last hour' })).toHaveAttribute(
      'aria-pressed',
      'false',
    )
    // The date input cannot show a time, so it shows the bound's UTC day and
    // editing it narrows the window to that day.
    expect(screen.getByLabelText('Since date')).toHaveValue(since.slice(0, 10))
  })
})
