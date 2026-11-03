import { StrictMode } from 'react'
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, RouterProvider, createMemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { api, ApiError } from '@/lib/api'
import { ServerSettingsPage } from '../ServerSettingsPage'

vi.mock('@/lib/api', () => ({
  api: { get: vi.fn(), put: vi.fn(), post: vi.fn(), delete: vi.fn(), testIntegration: vi.fn(), persistSetting: vi.fn() },
  extractApiDetail: (err: unknown) => {
    const detail = (err as { body?: { detail?: unknown } })?.body?.detail
    return typeof detail === 'string' ? detail : null
  },
  ApiError: class extends Error {
    status = 0
    statusText = ''
    body = null
  },
}))
vi.mock('@/lib/useSSE', () => ({ useSSE: vi.fn() }))
vi.mock('@/lib/auth', () => ({
  useAuth: () => ({
    isAdmin: true,
    username: 'admin',
    canUseServerProviders: false,
    canUseFreeProviders: false,
  }),
}))
vi.mock('@/hooks/useProviderModels', () => ({
  useProviderModels: () => ({ models: [], refresh: vi.fn(), refreshing: false }),
}))
vi.mock('@/lib/useProviderOptions', () => ({
  useCursorAuthStatus: () => null,
  // No catalog: the peer editor renders its empty pickers.
  useProviderCatalog: () => ({ providers: {}, providerStatus: {} }),
}))
vi.mock('@/components/shared/ServerProvidersCard', () => ({ ServerProvidersCard: () => null }))
vi.mock('@/components/shared/CursorAuthBanner', () => ({ CursorAuthBanner: () => null }))
vi.mock('@/components/shared/ProviderSelect', () => ({ ProviderSelect: () => null }))
vi.mock('@/components/shared/ModelCombobox', () => ({
  ModelCombobox: ({ className }: { className?: string }) => (
    <div data-testid="model-combobox" className={className} />
  ),
}))

interface TestSetting {
  key: string
  label: string
  env_var: string
  value: string
  default: string
  description: string
  type: 'string' | 'boolean' | 'integer'
  category: string
  sensitive: boolean
  is_set?: boolean
  restart_required: boolean
  source: 'default' | 'env' | 'db'
  updated_by: string
  updated_at: string
  options?: string[] | null
  options_source?: 'exporters' | 'providers' | 'users' | null
  multiple?: boolean
  allow_custom?: boolean
  jira_verified_method?: string
  persistable?: boolean
  derived?: boolean
}

function mk(key: string, category: string, value = '', extra: Partial<TestSetting> = {}): TestSetting {
  return {
    key,
    label: key.replace(/_/g, ' '),
    env_var: key.toUpperCase(),
    value,
    default: '',
    description: `Description for ${key}`,
    type: 'string',
    category,
    sensitive: false,
    restart_required: false,
    source: 'default',
    updated_by: '',
    updated_at: '',
    ...extra,
  }
}

const baseSettings = (): TestSetting[] => [
  mk('public_base_url', 'Server', 'https://rootcoz.example.com'),
  mk('wait_for_completion', 'Server', 'true', { type: 'boolean' }),
  mk('ai_provider', 'AI', 'gemini'),
  mk('ai_model', 'AI', 'gemini-2.5-pro'),
  mk('active_server_providers', 'AI', '', { options: ['gemini', 'openai', 'vertex-claude'] }),
  mk('jenkins_url', 'Jenkins', 'https://jenkins.example.com'),
  mk('jenkins_user', 'Jenkins', 'bot'),
  mk('jenkins_password', 'Jenkins', 'secret', { sensitive: true }),
  // Jira — jira_auth_mode (managed by the auth method selector), the five
  // shared settings, and each method's own credential/URL/SSL fields.
  mk('jira_auth_mode', 'Jira', ''),
  mk('jira_project_key', 'Jira', 'RC'),
  mk('jira_max_results', 'Jira', '5', { type: 'integer' }),
  mk('jira_site_url', 'Jira', 'https://rootcoz.atlassian.net'),
  mk('enable_jira', 'Jira', 'false', { type: 'boolean' }),
  mk('enable_jira_issues', 'Jira', 'false', { type: 'boolean' }),
  mk('jira_oauth_client_secret', 'Jira', '', { sensitive: true }),
  mk('atlassian_oauth_client_id', 'Jira', ''),
  mk('atlassian_oauth_cloud_id', 'Jira', ''),
  mk('jira_oauth_ssl_verify', 'Jira', 'true', { type: 'boolean' }),
  mk('jira_email', 'Jira', 'admin@example.com'),
  mk('jira_api_token', 'Jira', 'tok', { sensitive: true }),
  mk('jira_cloud_url', 'Jira', 'https://rootcoz.atlassian.net'),
  mk('jira_cloud_ssl_verify', 'Jira', 'true', { type: 'boolean' }),
  mk('jira_pat', 'Jira', '', { sensitive: true }),
  mk('jira_atst_token', 'Jira', '', { sensitive: true }),
  mk('jira_server_url', 'Jira', 'https://jira.example.com'),
  mk('jira_server_ssl_verify', 'Jira', 'true', { type: 'boolean' }),
  mk('github_token', 'GitHub', 'gh_tok', { sensitive: true }),
  mk('reportportal_url', 'Report Portal', 'https://rp.example.com'),
  mk('datadog_api_key', 'Datadog', '', { sensitive: true }),
  mk('enable_datadog', 'Datadog', 'false', { type: 'boolean' }),
  mk('prow_url', 'Prow', 'https://prow.example.com'),
  mk('admin_key', 'Auth & Security', 'key', { sensitive: true }),
  mk('vapid_public_key', 'Web Push', 'vk'),
]

const get = vi.mocked(api.get)
const put = vi.mocked(api.put)
const post = vi.mocked(api.post)
const testIntegration = vi.mocked(api.testIntegration)
const persistSetting = vi.mocked(api.persistSetting)

function renderPage(entry = '/admin/settings') {
  return render(
    <MemoryRouter initialEntries={[entry]}>
      <ServerSettingsPage />
    </MemoryRouter>,
  )
}

/** A router the test can navigate, like following an audit row's link. */
function renderRouted(entry = '/admin/settings') {
  const router = createMemoryRouter([{ path: '/admin/settings', element: <ServerSettingsPage /> }], {
    initialEntries: [entry],
  })
  return { router, ...render(<RouterProvider router={router} />) }
}

/**
 * A row is found by its key: the env var lives in the label tooltip, never in
 * row text (E2E-F27), so `screen.getByText('JIRA_PAT')` is no longer the way.
 */
const row = (key: string) => document.querySelector(`[data-setting-key="${key}"]`) as HTMLElement | null
const shown = (key: string) => row(key) !== null
const waitForRow = (key: string) => waitFor(() => expect(shown(key)).toBe(true))

// jsdom has no layout, so scrolling a row into view is a no-op that must still
// be callable — and observable, to prove the link actually lands on the row.
const scrollIntoView = vi.fn()
HTMLElement.prototype.scrollIntoView = scrollIntoView

// Every enumerated setting (single or multi) is the same searchable picker, so
// it opens and picks the same way — no radix Select is left on this page.
const singlePicker = (name: string) => screen.getByRole('combobox', { name: `Select ${name}` })

function pickSingle(name: string, optionName: string) {
  fireEvent.click(singlePicker(name))
  // mouseDown is what the menu listens for (it keeps focus in the search field).
  fireEvent.mouseDown(screen.getByRole('option', { name: optionName }))
}

const jiraAuthSelect = () => singlePicker('Jira auth method')
const chooseJiraMethod = (label: string) => pickSingle('Jira auth method', label)

/** Open a searchable multi-select (it opens on click, like a real click). */
function openPicker(envVar: string) {
  fireEvent.click(screen.getByRole('combobox', { name: `Add ${envVar}` }))
}

const searchBox = (envVar: string) =>
  screen.getByRole('combobox', { name: `Search ${envVar}` })

const optionNames = () =>
  screen.getAllByRole('option').map((option) => option.textContent)

beforeEach(() => {
  vi.clearAllMocks()
  localStorage.clear()
  scrollIntoView.mockClear()
  get.mockResolvedValue(baseSettings() as never)
  put.mockResolvedValue({} as never)
  testIntegration.mockResolvedValue({ ok: true, message: 'ok' } as never)
  persistSetting.mockResolvedValue({ key: '', source: 'db' } as never)
})

describe('ServerSettingsPage tabs and search', () => {
  it('renders categories as tabs in the specified order and switches content', async () => {
    renderPage()
    await waitForRow('public_base_url')

    const tabs = screen.getAllByRole('tab')
    expect(tabs.map((t) => t.textContent)).toEqual([
      'Server', 'AI', 'Jenkins', 'Jira', 'GitHub', 'Report Portal', 'Datadog', 'Prow',
      'Auth & Security', 'Web Push',
    ])

    // Default (Server) tab shows only Server settings
    expect(shown('jenkins_url')).toBe(false)

    fireEvent.click(screen.getByRole('tab', { name: 'Jira' }))
    await waitForRow('jira_project_key')
    expect(shown('public_base_url')).toBe(false)

    // Active tab persists to localStorage
    expect(localStorage.getItem('rootcoz_settings_tab')).toBe('Jira')
  })

  it('opens, scrolls to and marks the setting named by ?highlight=', async () => {
    // The admin's remembered tab is Server; the linked setting lives elsewhere.
    localStorage.setItem('rootcoz_settings_tab', 'Server')
    renderPage('/admin/settings?highlight=admin_key')

    const row = await waitFor(() => {
      const found = document.querySelector('[data-setting-key="admin_key"]')
      expect(found).not.toBeNull()
      return found as HTMLElement
    })
    expect(row.className).toContain('bg-signal-blue/10')
    expect(scrollIntoView).toHaveBeenCalled()
    // Following an audit link must not rewrite the admin's own tab preference.
    expect(localStorage.getItem('rootcoz_settings_tab')).toBe('Server')
  })

  it('clears a search that hides the linked row before marking it', async () => {
    // The admin is searching; the audit link names a row that search excludes.
    const { router } = renderRouted()
    await waitForRow('public_base_url')
    const search = screen.getByLabelText('Search settings')
    fireEvent.change(search, { target: { value: 'jenkins_url' } })
    await waitFor(() => expect(screen.getByText('Search results')).toBeInTheDocument())
    expect(shown('admin_key')).toBe(false)

    await act(async () => {
      await router.navigate('/admin/settings?highlight=admin_key')
    })

    // The search is gone, so the row exists again — and the mark lands on it.
    await waitFor(() => expect(search).toHaveValue(''))
    const row = await waitFor(() => {
      const found = document.querySelector('[data-setting-key="admin_key"]')
      expect(found).not.toBeNull()
      return found as HTMLElement
    })
    expect(row.className).toContain('bg-signal-blue/10')
    expect(scrollIntoView).toHaveBeenCalled()
    expect(screen.queryByText('Search results')).toBeNull()
  })

  it('switches to a flat search results view when searching', async () => {
    renderPage()
    await waitForRow('public_base_url')

    fireEvent.change(screen.getByLabelText('Search settings'), { target: { value: 'jenkins_url' } })
    await waitFor(() => expect(screen.queryByRole('tab')).toBeNull())

    expect(screen.getByText('Search results')).toBeInTheDocument()
    expect(shown('jenkins_url')).toBe(true)
    expect(shown('jira_project_key')).toBe(false)
  })

  it('matches every whitespace-separated word of the query', async () => {
    get.mockResolvedValue([
      // Real shape: the words of the key are one token apart and the
      // description carries the rest.
      mk('audit_retention_days', 'Auth & Security', '90', {
        type: 'integer',
        description: 'How many days of audit events to keep on this server.',
      }),
      mk('jenkins_url', 'Jenkins', 'https://jenkins.example.com'),
    ] as never)
    renderPage()
    await waitForRow('jenkins_url')
    const search = screen.getByLabelText('Search settings')

    // Two tokens of the key — the raw substring 'audit retention' is not present.
    fireEvent.change(search, { target: { value: '  audit   retention ' } })
    await waitFor(() => expect(screen.getByText('Search results')).toBeInTheDocument())
    expect(shown('audit_retention_days')).toBe(true)
    expect(shown('jenkins_url')).toBe(false)

    // Tokens from the key and the description match together too.
    fireEvent.change(search, { target: { value: 'retention days' } })
    await waitFor(() => expect(screen.getByText('Search results')).toBeInTheDocument())
    expect(shown('audit_retention_days')).toBe(true)

    // 'data' appears in no searchable field of this setting, so AND semantics
    // must keep it out — this is the documented behaviour, not a miss.
    fireEvent.change(search, { target: { value: 'data retention' } })
    expect(await screen.findByText('No settings match your search.')).toBeInTheDocument()

    // Single token is unchanged.
    fireEvent.change(search, { target: { value: 'audit' } })
    await waitFor(() => expect(shown('audit_retention_days')).toBe(true))

    // The category is searchable too — this setting lives under "Auth & Security".
    fireEvent.change(search, { target: { value: 'audit security' } })
    await waitFor(() => expect(shown('audit_retention_days')).toBe(true))
    expect(shown('jenkins_url')).toBe(false)

    // Whitespace-only is still no query at all.
    fireEvent.change(search, { target: { value: '   ' } })
    await waitFor(() => expect(shown('jenkins_url')).toBe(true))
    expect(shown('audit_retention_days')).toBe(true)
  })
})

describe('ServerSettingsPage silent refetch on save', () => {
  it('keeps content mounted after a save (no loading skeleton re-appears)', async () => {
    renderPage()
    await waitForRow('public_base_url')
    expect(get).toHaveBeenCalledTimes(1)

    // Toggle a boolean on the Server tab → immediate save + silent refetch
    fireEvent.click(screen.getByRole('switch', { name: 'Toggle WAIT_FOR_COMPLETION' }))
    await waitFor(() => expect(put).toHaveBeenCalledTimes(1))
    await waitFor(() => expect(get).toHaveBeenCalledTimes(2))

    // Content stayed mounted: rows still present, no skeleton
    expect(shown('public_base_url')).toBe(true)
    expect(document.querySelector('.animate-pulse')).toBeNull()
    expect(screen.getByRole('tab', { name: 'Server' })).toBeInTheDocument()
  })
})

// Settings whose jira_auth_mode value updates on save, so the refetched
// state reflects what the UI persisted. `verified` is the jira_verified_method
// the server reports on the jira_auth_mode item (defaults to the mode: tested+passed).
function mockJiraModeSettings(mode: string, verified: string | null = mode) {
  let current = mode
  let verifiedMethod = verified ?? ''
  get.mockImplementation(async () => {
    const settings = baseSettings()
    settings.find((s) => s.key === 'jira_auth_mode')!.value = current
    settings.find((s) => s.key === 'jira_auth_mode')!.jira_verified_method = verifiedMethod
    return settings
  })
  put.mockImplementation(async (_url: string, body?: unknown) => {
    const settings = (body as { settings?: Record<string, string> })?.settings ?? {}
    if (settings.jira_auth_mode !== undefined) current = settings.jira_auth_mode
    return {}
  })
  return { setVerified: (v: string) => { verifiedMethod = v } }
}

describe('ServerSettingsPage Jira auth method selection', () => {
  async function openJiraTab() {
    renderPage()
    fireEvent.click(await screen.findByRole('tab', { name: 'Jira' }))
    await waitForRow('jira_project_key')
  }

  it('shows the derived oauth API base under the oauth method (#352)', async () => {
    mockJiraModeSettings('oauth')
    get.mockImplementation(async () => {
      const settings = baseSettings()
      settings.find(s => s.key === 'jira_auth_mode')!.value = 'oauth'
      settings.find(s => s.key === 'jira_auth_mode')!.jira_verified_method = 'oauth'
      settings.find(s => s.key === 'atlassian_oauth_cloud_id')!.value = 'test-cloud-id-1234'
      return settings
    })
    await openJiraTab()

    // Read-only derived row: the scoped gateway built from the cloud ID
    expect(screen.getByText('JIRA_OAUTH_URL')).toBeInTheDocument()
    expect(
      screen.getByText('https://api.atlassian.com/ex/jira/test-cloud-id-1234')
    ).toBeInTheDocument()
  })

  it('warns when the oauth cloud ID is missing so the URL cannot be derived', async () => {
    mockJiraModeSettings('oauth')
    await openJiraTab()

    expect(screen.getByText('JIRA_OAUTH_URL')).toBeInTheDocument()
    expect(
      screen.getByText(/ATLASSIAN_OAUTH_CLOUD_ID is not set/)
    ).toBeInTheDocument()
    expect(
      screen.queryByText(/api\.atlassian\.com\/ex\/jira\//)
    ).not.toBeInTheDocument()
  })

  it('hides the derived oauth URL row under non-oauth methods', async () => {
    mockJiraModeSettings('basic')
    await openJiraTab()

    expect(screen.queryByText('JIRA_OAUTH_URL')).not.toBeInTheDocument()
  })

  it('checkbox state reflects the persisted jira_auth_mode value and shows only that method\'s fields', async () => {
    mockJiraModeSettings('basic')
    await openJiraTab()

    expect(jiraAuthSelect()).toHaveTextContent('Email + API token')
    expect(jiraAuthSelect()).not.toHaveTextContent('PAT (Bearer)')
    expect(jiraAuthSelect()).not.toHaveTextContent('Atlassian OAuth (client secret / cloud ID)')

    // Basic's four fields render (the SSL toggle renders as a switch)
    expect(shown('jira_email')).toBe(true)
    expect(shown('jira_api_token')).toBe(true)
    expect(shown('jira_cloud_url')).toBe(true)
    expect(screen.getByRole('switch', { name: 'Toggle JIRA_CLOUD_SSL_VERIFY' })).toBeInTheDocument()

    // Other methods' fields are hidden
    expect(shown('jira_pat')).toBe(false)
    expect(shown('jira_server_url')).toBe(false)
    expect(shown('jira_server_ssl_verify')).toBe(false)
    expect(shown('jira_oauth_client_secret')).toBe(false)
    expect(shown('atlassian_oauth_cloud_id')).toBe(false)
    expect(shown('atlassian_oauth_client_id')).toBe(false)
    expect(screen.queryByRole('switch', { name: 'Toggle JIRA_OAUTH_SSL_VERIFY' })).not.toBeInTheDocument()

    // The shared settings stay visible regardless of method
    expect(shown('jira_project_key')).toBe(true)
    expect(shown('jira_max_results')).toBe(true)
    expect(shown('jira_site_url')).toBe(true)
    expect(screen.getByRole('switch', { name: 'Toggle ENABLE_JIRA' })).toBeInTheDocument()
    expect(screen.getByRole('switch', { name: 'Toggle ENABLE_JIRA_ISSUES' })).toBeInTheDocument()

    // The removed legacy settings no longer exist anywhere
    expect(shown('jira_url')).toBe(false)
    expect(shown('jira_ssl_verify')).toBe(false)
  })

  it('asks for confirmation when switching away from an active method; cancel keeps the old one', async () => {
    mockJiraModeSettings('basic')
    await openJiraTab()

    chooseJiraMethod('PAT (Bearer)')
    const dialog = await screen.findByRole('alertdialog')
    expect(dialog).toHaveTextContent('Switch from Email + API token to PAT (Bearer)?')
    expect(put).not.toHaveBeenCalled()

    fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(screen.queryByRole('alertdialog')).toBeNull())
    expect(put).not.toHaveBeenCalled()
    expect(jiraAuthSelect()).toHaveTextContent('Email + API token')
    expect(jiraAuthSelect()).not.toHaveTextContent('PAT (Bearer)')
  })

  it('confirm applies the switch: saves jira_auth_mode and unchecks the other method', async () => {
    mockJiraModeSettings('basic')
    await openJiraTab()

    chooseJiraMethod('PAT (Bearer)')
    fireEvent.click(within(await screen.findByRole('alertdialog')).getByRole('button', { name: 'Switch' }))

    await waitFor(() =>
      expect(put).toHaveBeenCalledWith('/api/admin/settings', { settings: { jira_auth_mode: 'pat' } }),
    )
    await waitFor(() => expect(jiraAuthSelect()).toHaveTextContent('PAT (Bearer)'))
    expect(jiraAuthSelect()).not.toHaveTextContent('Email + API token')
    expect(shown('jira_pat')).toBe(true)
    expect(shown('jira_email')).toBe(false)
  })

  it('selecting a method when none is active applies without confirmation', async () => {
    mockJiraModeSettings('')
    await openJiraTab()
    expect(screen.queryByRole('alertdialog')).toBeNull()

    // No method selected: no method credential fields render, but
    // method-independent rows do
    expect(shown('jira_pat')).toBe(false)
    expect(shown('jira_server_url')).toBe(false)
    expect(shown('jira_email')).toBe(false)
    expect(shown('jira_cloud_url')).toBe(false)
    expect(shown('jira_oauth_client_secret')).toBe(false)
    expect(shown('atlassian_oauth_cloud_id')).toBe(false)
    expect(shown('atlassian_oauth_client_id')).toBe(false)
    expect(screen.queryByRole('switch', { name: 'Toggle JIRA_OAUTH_SSL_VERIFY' })).not.toBeInTheDocument()
    expect(shown('jira_project_key')).toBe(true)
    expect(shown('jira_site_url')).toBe(true)

    chooseJiraMethod('Atlassian OAuth (client secret / cloud ID)')
    expect(screen.queryByRole('alertdialog')).toBeNull()

    await waitFor(() =>
      expect(put).toHaveBeenCalledWith('/api/admin/settings', { settings: { jira_auth_mode: 'oauth' } }),
    )
    await waitFor(() =>
      expect(jiraAuthSelect()).toHaveTextContent('Atlassian OAuth (client secret / cloud ID)'),
    )
    expect(jiraAuthSelect()).not.toHaveTextContent('Email + API token')
    expect(shown('jira_oauth_client_secret')).toBe(true)
    expect(shown('atlassian_oauth_cloud_id')).toBe(true)
    expect(shown('atlassian_oauth_client_id')).toBe(true)
    expect(screen.getByRole('switch', { name: 'Toggle JIRA_OAUTH_SSL_VERIFY' })).toBeInTheDocument()
    expect(shown('jira_pat')).toBe(false)
    expect(shown('jira_email')).toBe(false)

    // Shared fields carry a hint that they are shared with the atst method
    expect(
      screen.getAllByText(
        'Shared with the Atlassian service account token (ATST, Bearer) method — both target the same Atlassian gateway tenant.',
      ),
    ).toHaveLength(2)
    // Non-shared rows (e.g. the oauth client secret) do not carry the hint
    expect(row('jira_oauth_client_secret')).not.toHaveTextContent(/Shared with the/)
  })

  it('selecting the atst method shows its fields and hides the other methods\' credentials', async () => {
    mockJiraModeSettings('')
    await openJiraTab()
    expect(screen.queryByRole('alertdialog')).toBeNull()

    chooseJiraMethod('Atlassian service account token (ATST, Bearer)')

    await waitFor(() =>
      expect(put).toHaveBeenCalledWith('/api/admin/settings', { settings: { jira_auth_mode: 'atst' } }),
    )
    await waitFor(() =>
      expect(jiraAuthSelect()).toHaveTextContent('Atlassian service account token (ATST, Bearer)'),
    )
    expect(shown('jira_atst_token')).toBe(true)
    // Shared locator/TLS fields stay visible for atst (same gateway as oauth)
    expect(shown('atlassian_oauth_cloud_id')).toBe(true)
    expect(screen.getByRole('switch', { name: 'Toggle JIRA_OAUTH_SSL_VERIFY' })).toBeInTheDocument()
    // Shared fields carry a hint that they are shared with the oauth method
    expect(
      screen.getAllByText(
        'Shared with the Atlassian OAuth (client secret / cloud ID) method — both target the same Atlassian gateway tenant.',
      ),
    ).toHaveLength(2)
    // Non-shared rows (e.g. the ATST token) do not carry the hint
    expect(row('jira_atst_token')).not.toHaveTextContent(/Shared with the/)
    // Other methods' credentials are hidden
    expect(shown('jira_oauth_client_secret')).toBe(false)
    expect(shown('atlassian_oauth_client_id')).toBe(false)
    expect(shown('jira_pat')).toBe(false)
    expect(shown('jira_email')).toBe(false)
  })

  it('switching away from a selected-but-unverified method applies immediately without confirmation', async () => {
    // basic is selected but its Test connection never passed (verified = '')
    mockJiraModeSettings('basic', '')
    await openJiraTab()

    chooseJiraMethod('PAT (Bearer)')
    expect(screen.queryByRole('alertdialog')).toBeNull()

    await waitFor(() =>
      expect(put).toHaveBeenCalledWith('/api/admin/settings', { settings: { jira_auth_mode: 'pat' } }),
    )
    await waitFor(() => expect(jiraAuthSelect()).toHaveTextContent('PAT (Bearer)'))
  })

  // ---- Refinement 6: the switch auto-runs the Jira test for the new method ----

  it('auto-runs the Jira test after switching to a configured method and shows the success state', async () => {
    mockJiraModeSettings('basic')
    testIntegration.mockResolvedValue({ ok: true, message: 'Jira connection verified' } as never)
    await openJiraTab()

    chooseJiraMethod('PAT (Bearer)')
    fireEvent.click(within(await screen.findByRole('alertdialog')).getByRole('button', { name: 'Switch' }))

    await waitFor(() =>
      expect(put).toHaveBeenCalledWith('/api/admin/settings', { settings: { jira_auth_mode: 'pat' } }),
    )
    // The test POST fires automatically for the newly selected method
    await waitFor(() => expect(testIntegration).toHaveBeenCalledWith('jira'))
    const banner = await screen.findByRole('status')
    expect(banner).toHaveTextContent('Connection successful')
    expect(banner).toHaveTextContent('Jira connection verified')
    // The switch is never auto-reverted on the admin's behalf
    expect(jiraAuthSelect()).toHaveTextContent('PAT (Bearer)')
  })

  it('auto-runs the Jira test after switching to an unconfigured method and surfaces the missing-fields error', async () => {
    mockJiraModeSettings('basic')
    testIntegration.mockResolvedValue({
      ok: false,
      message: "Jira auth method 'pat' requires jira_pat",
    } as never)
    await openJiraTab()

    chooseJiraMethod('PAT (Bearer)')
    fireEvent.click(within(await screen.findByRole('alertdialog')).getByRole('button', { name: 'Switch' }))

    await waitFor(() =>
      expect(put).toHaveBeenCalledWith('/api/admin/settings', { settings: { jira_auth_mode: 'pat' } }),
    )
    await waitFor(() => expect(testIntegration).toHaveBeenCalledWith('jira'))
    const banner = await screen.findByRole('status')
    expect(banner).toHaveTextContent('Connection failed')
    expect(banner).toHaveTextContent("requires jira_pat")
    // The failed auto-test surfaces the error but keeps the selection
    expect(jiraAuthSelect()).toHaveTextContent('PAT (Bearer)')
  })

  it('does not run any test when switching to none (disabling Jira)', async () => {
    mockJiraModeSettings('basic')
    await openJiraTab()

    chooseJiraMethod('None — Jira disabled')
    fireEvent.click(within(await screen.findByRole('alertdialog')).getByRole('button', { name: 'Disable' }))

    await waitFor(() =>
      expect(put).toHaveBeenCalledWith('/api/admin/settings', { settings: { jira_auth_mode: '' } }),
    )
    await waitFor(() =>
      expect(jiraAuthSelect()).not.toHaveTextContent('Email + API token'),
    )
    expect(testIntegration).not.toHaveBeenCalled()
  })

  it('still auto-runs the test for the target method after confirming a switch away from a verified method', async () => {
    // basic is verified, so switching to pat goes through the confirmation dialog
    mockJiraModeSettings('basic')
    testIntegration.mockResolvedValue({ ok: true, message: 'ok' } as never)
    await openJiraTab()

    chooseJiraMethod('PAT (Bearer)')
    fireEvent.click(within(await screen.findByRole('alertdialog')).getByRole('button', { name: 'Switch' }))

    await waitFor(() => expect(testIntegration).toHaveBeenCalledTimes(1))
    expect(testIntegration).toHaveBeenCalledWith('jira')
    await screen.findByRole('status') // success banner from the auto-test
  })

  it('a successful Jira test refreshes the verified method so the next switch asks for confirmation', async () => {
    const jira = mockJiraModeSettings('basic', '')
    testIntegration.mockImplementation(async () => {
      jira.setVerified('basic') // backend records the method whose test succeeded
      return { ok: true, message: 'ok' } as never
    })
    await openJiraTab()
    expect(get).toHaveBeenCalledTimes(1)

    fireEvent.click(screen.getByRole('button', { name: /test connection/i }))
    await screen.findByRole('status') // success banner
    await waitFor(() => expect(get).toHaveBeenCalledTimes(2)) // silent metadata refetch

    // Now that basic is verified, switching away is confirmed, not silent
    chooseJiraMethod('PAT (Bearer)')
    const dialog = await screen.findByRole('alertdialog')
    expect(dialog).toHaveTextContent('Switch from Email + API token to PAT (Bearer)?')
    expect(put).not.toHaveBeenCalled()
  })

  it('a stale settings fetch resolving after a method switch does not overwrite the switch result', async () => {
    // basic selected, unverified → the Jira test verifies it server-side.
    const jira = mockJiraModeSettings('basic', '')
    // The manual test verifies basic; the post-switch AUTO-test (#346) is made
    // to fail here so it adds no success refetch that would muddy get counts.
    testIntegration.mockImplementationOnce(async () => {
      jira.setVerified('basic') // backend records the method whose test succeeded
      return { ok: true, message: 'ok' } as never
    })
    testIntegration.mockImplementation(
      async () => ({ ok: false, message: 'auto-test failed' } as never),
    )
    await openJiraTab()
    expect(get).toHaveBeenCalledTimes(1)

    // The test-triggered metadata refetch stays pending (captures OLD state)
    let resolveStale!: (value: TestSetting[]) => void
    get.mockImplementationOnce(
      () => new Promise<TestSetting[]>((r) => { resolveStale = r }),
    )

    fireEvent.click(screen.getByRole('button', { name: /test connection/i }))
    await screen.findByRole('status') // success banner
    await waitFor(() => expect(get).toHaveBeenCalledTimes(2)) // stale fetch in flight

    // Switch basic → pat while the stale fetch is pending (unverified in UI
    // state, so no confirmation dialog). The switch's own refetch resolves first.
    chooseJiraMethod('PAT (Bearer)')
    await waitFor(() =>
      expect(put).toHaveBeenCalledWith('/api/admin/settings', { settings: { jira_auth_mode: 'pat' } }),
    )
    await waitFor(() => expect(jiraAuthSelect()).toHaveTextContent('PAT (Bearer)'))
    expect(shown('jira_pat')).toBe(true)
    expect(shown('jira_email')).toBe(false)

    // The stale response (old method basic, now verified) arrives LAST —
    // it must be dropped: final state still reflects the pat switch.
    const stale = baseSettings()
    stale.find((s) => s.key === 'jira_auth_mode')!.value = 'basic'
    stale.find((s) => s.key === 'jira_auth_mode')!.jira_verified_method = 'basic'
    resolveStale(stale)
    await waitFor(() => expect(get).toHaveBeenCalledTimes(3))

    expect(jiraAuthSelect()).toHaveTextContent('PAT (Bearer)')
    expect(jiraAuthSelect()).not.toHaveTextContent('Email + API token')
    expect(shown('jira_pat')).toBe(true)
    expect(shown('jira_email')).toBe(false)
  })

  it('choosing none disables Jira immediately when the method was never verified (no method fields render while unset)', async () => {
    mockJiraModeSettings('basic', '')
    await openJiraTab()

    chooseJiraMethod('None — Jira disabled')
    expect(screen.queryByRole('alertdialog')).toBeNull()
    await waitFor(() =>
      expect(put).toHaveBeenCalledWith('/api/admin/settings', { settings: { jira_auth_mode: '' } }),
    )
    await waitFor(() =>
      expect(jiraAuthSelect()).not.toHaveTextContent('Email + API token'),
    )
    expect(jiraAuthSelect()).toHaveTextContent('None — Jira disabled')
    expect(shown('jira_pat')).toBe(false)
    expect(shown('jira_email')).toBe(false)
    expect(shown('jira_oauth_client_secret')).toBe(false)
    // Method-independent rows stay visible
    expect(shown('jira_project_key')).toBe(true)
  })

  it('choosing none for a verified method asks for confirmation and disables Jira on confirm', async () => {
    mockJiraModeSettings('basic') // basic selected and test-verified
    await openJiraTab()

    chooseJiraMethod('None — Jira disabled')
    const dialog = await screen.findByRole('alertdialog')
    expect(dialog).toHaveTextContent(/disables Jira until another method is selected/)
    expect(put).not.toHaveBeenCalled()

    fireEvent.click(within(dialog).getByRole('button', { name: 'Disable' }))
    await waitFor(() =>
      expect(put).toHaveBeenCalledWith('/api/admin/settings', { settings: { jira_auth_mode: '' } }),
    )
    await waitFor(() =>
      expect(jiraAuthSelect()).not.toHaveTextContent('Email + API token'),
    )
    expect(jiraAuthSelect()).toHaveTextContent('None — Jira disabled')
  })

  it('cancelling the disable confirmation keeps the method selected', async () => {
    mockJiraModeSettings('basic')
    await openJiraTab()

    chooseJiraMethod('None — Jira disabled')
    fireEvent.click(within(await screen.findByRole('alertdialog')).getByRole('button', { name: 'Cancel' }))

    await waitFor(() => expect(screen.queryByRole('alertdialog')).toBeNull())
    expect(put).not.toHaveBeenCalled()
    expect(jiraAuthSelect()).toHaveTextContent('Email + API token')
  })

  it('surfaces a backend save-validation error in the Jira section and keeps the selection', async () => {
    mockJiraModeSettings('basic')
    const err = new ApiError(400, 'Bad Request', null)
    err.status = 400
    err.body = { detail: "Jira auth method 'pat' requires jira_pat" }
    put.mockRejectedValueOnce(err as never)

    await openJiraTab()
    chooseJiraMethod('PAT (Bearer)')
    fireEvent.click(within(await screen.findByRole('alertdialog')).getByRole('button', { name: 'Switch' }))

    const alerts = await screen.findAllByRole('alert')
    expect(alerts.some((a) => a.textContent?.includes("requires jira_pat"))).toBe(true)
    // The failed switch is not applied
    expect(jiraAuthSelect()).toHaveTextContent('Email + API token')
    expect(jiraAuthSelect()).not.toHaveTextContent('PAT (Bearer)')
  })

  it('shows the disabled hint when no method is selected', async () => {
    mockJiraModeSettings('')
    await openJiraTab()
    expect(jiraAuthSelect()).toHaveTextContent('None — Jira disabled')
    // The hint never checks a box — only the persisted setting does
    expect(jiraAuthSelect()).not.toHaveTextContent('Email + API token')
  })

  it('hides the disabled hint when a method is explicitly selected', async () => {
    mockJiraModeSettings('pat')
    await openJiraTab()
    expect(jiraAuthSelect()).not.toHaveTextContent('None — Jira disabled')
    expect(jiraAuthSelect()).toHaveTextContent('PAT (Bearer)')
  })

  it('surfaces the backend error when testing with no method selected', async () => {
    mockJiraModeSettings('')
    testIntegration.mockResolvedValue({
      ok: false,
      message: 'No Jira auth method selected — choose one in Server Settings',
    } as never)
    await openJiraTab()

    fireEvent.click(screen.getByRole('button', { name: /test connection/i }))
    const banner = await screen.findByRole('status')
    expect(banner).toHaveTextContent('Connection failed')
    expect(banner).toHaveTextContent('No Jira auth method selected — choose one in Server Settings')
  })
})

describe('ServerSettingsPage test connection buttons', () => {
  it('renders test buttons only for the six integrable categories', async () => {
    renderPage()
    await waitForRow('public_base_url')

    for (const tab of ['Jenkins', 'Jira', 'GitHub', 'Report Portal', 'Datadog', 'Prow']) {
      fireEvent.click(screen.getByRole('tab', { name: tab }))
      expect(await screen.findByRole('button', { name: /test connection/i })).toBeInTheDocument()
    }
    for (const tab of ['Server', 'AI', 'Auth & Security', 'Web Push']) {
      fireEvent.click(screen.getByRole('tab', { name: tab }))
      await waitFor(() =>
        expect(screen.queryByRole('button', { name: /test connection/i })).toBeNull(),
      )
    }
  })

  it('shows a success banner with message and details, dismissed on tab change', async () => {
    testIntegration.mockResolvedValue({
      ok: true,
      message: 'Jenkins reachable',
      details: { url: 'https://jenkins.example.com' },
    } as never)

    renderPage()
    fireEvent.click(await screen.findByRole('tab', { name: 'Jenkins' }))
    fireEvent.click(await screen.findByRole('button', { name: /test connection/i }))

    const banner = await screen.findByRole('status')
    expect(banner).toHaveTextContent('Connection successful')
    expect(banner).toHaveTextContent('Jenkins reachable')
    expect(banner).toHaveTextContent('jenkins.example.com')
    expect(testIntegration).toHaveBeenCalledWith('jenkins')

    // Banner dismisses on tab change
    fireEvent.click(screen.getByRole('tab', { name: 'Jira' }))
    await waitFor(() => expect(screen.queryByRole('status')).toBeNull())
  })

  it('shows a failure banner from the mocked response', async () => {
    testIntegration.mockResolvedValue({ ok: false, message: 'Connection refused' } as never)

    renderPage()
    fireEvent.click(await screen.findByRole('tab', { name: 'Jira' }))
    fireEvent.click(await screen.findByRole('button', { name: /test connection/i }))

    const banner = await screen.findByRole('status')
    expect(banner).toHaveTextContent('Connection failed')
    expect(banner).toHaveTextContent('Connection refused')
    expect(testIntegration).toHaveBeenCalledWith('jira')
  })

  it('does not re-show a test banner that resolves after a tab change', async () => {
    let resolveTest!: (value: { ok: boolean; message: string }) => void
    testIntegration.mockReturnValueOnce(
      new Promise((r) => {
        resolveTest = r
      }) as never,
    )

    renderPage()
    fireEvent.click(await screen.findByRole('tab', { name: 'Jenkins' }))
    fireEvent.click(await screen.findByRole('button', { name: /test connection/i }))

    // Switch tabs while the test is still in flight
    fireEvent.click(screen.getByRole('tab', { name: 'Jira' }))
    resolveTest({ ok: true, message: 'late success' })
    await waitFor(() => expect(screen.queryByRole('status')).toBeNull())

    // Returning to the tab must not show the stale result
    fireEvent.click(screen.getByRole('tab', { name: 'Jenkins' }))
    expect(await screen.findByRole('button', { name: /test connection/i })).toBeEnabled()
    expect(screen.queryByRole('status')).toBeNull()
  })

  it('disables the test button while a field in that section is being edited', async () => {
    renderPage()
    fireEvent.click(await screen.findByRole('tab', { name: 'Jenkins' }))

    const testButton = await screen.findByRole('button', { name: /test connection/i })
    expect(testButton).toBeEnabled()

    fireEvent.click(screen.getByRole('button', { name: 'Edit JENKINS_USER' }))
    expect(screen.getByRole('button', { name: /test connection/i })).toBeDisabled()

    // Cancel editing → re-enabled
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(screen.getByRole('button', { name: /test connection/i })).toBeEnabled())
  })
})

describe('ServerSettingsPage active server providers', () => {
  async function openAiTab() {
    renderPage()
    fireEvent.click(await screen.findByRole('tab', { name: 'AI' }))
    await waitForRow('active_server_providers')
  }

  function restricted(value: string, extra: Partial<TestSetting> = {}): TestSetting[] {
    const settings = baseSettings()
    Object.assign(settings.find((s) => s.key === 'active_server_providers')!, { value, ...extra })
    return settings
  }

  function providerBoxes(): HTMLElement[] {
    return ['gemini', 'openai', 'vertex-claude'].map(
      (p) => screen.getByRole('checkbox', { name: p }) as HTMLElement,
    )
  }

  it('renders the provider checkboxes immediately, all checked for an empty value', async () => {
    await openAiTab()

    for (const box of providerBoxes()) expect(box).toBeChecked()
    expect(screen.queryByRole('button', { name: 'Configure providers' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Save' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Cancel' })).not.toBeInTheDocument()
  })

  it('renders the ambient auth provider multiselect, which admins now own (#379)', async () => {
    get.mockImplementation(async (url: unknown) => {
      const settings = [
        ...baseSettings(),
        mk('ambient_auth_providers', 'AI', 'vertex-claude', {
          options: null,
          options_source: 'providers',
          multiple: true,
        }),
      ]
      if (String(url) === '/api/ai-models') return { providers: { 'vertex-claude': [] } } as never
      return settings as never
    })

    await openAiTab()
    expect(shown('ambient_auth_providers')).toBe(true)
    expect(screen.getByRole('button', { name: 'Remove vertex-claude' })).toBeInTheDocument()
  })

  it('renders the stored restriction as the checked subset', async () => {
    get.mockResolvedValue(restricted('gemini,vertex-claude') as never)

    await openAiTab()
    const [gemini, openai, claude] = providerBoxes()
    expect(gemini).toBeChecked()
    expect(openai).not.toBeChecked()
    expect(claude).toBeChecked()
  })

  it('toggling changes only the draft — no save fires before Save', async () => {
    await openAiTab()

    fireEvent.click(screen.getByRole('checkbox', { name: 'openai' }))
    expect(screen.getByRole('checkbox', { name: 'openai' })).not.toBeChecked()
    expect(screen.getByRole('button', { name: 'Save' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Cancel' })).toBeInTheDocument()
    expect(put).not.toHaveBeenCalled()
  })

  it('Save persists the checked ids when some providers are unchecked', async () => {
    await openAiTab()

    fireEvent.click(screen.getByRole('checkbox', { name: 'openai' }))
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))

    await waitFor(() =>
      expect(put).toHaveBeenCalledWith('/api/admin/settings', {
        settings: { active_server_providers: 'gemini,vertex-claude' },
      }),
    )
  })

  it('checking every visible provider saves all-active even when discovery omits a stored id (#380)', async () => {
    // vertex-claude is stored but no longer discovered: it has no checkbox, and
    // all visible providers checked means "all active" (empty value).
    get.mockResolvedValue(
      restricted('gemini,vertex-claude', { options: ['gemini', 'openai'] }) as never,
    )

    await openAiTab()
    expect(screen.queryByRole('checkbox', { name: 'vertex-claude' })).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('checkbox', { name: 'openai' }))
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))

    await waitFor(() =>
      expect(put).toHaveBeenCalledWith('/api/admin/settings', {
        settings: { active_server_providers: '' },
      }),
    )
  })

  it('Save persists an empty value when everything ends up checked (all active)', async () => {
    get.mockResolvedValue(restricted('gemini') as never)

    await openAiTab()
    fireEvent.click(screen.getByRole('checkbox', { name: 'openai' }))
    fireEvent.click(screen.getByRole('checkbox', { name: 'vertex-claude' }))
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))

    await waitFor(() =>
      expect(put).toHaveBeenCalledWith('/api/admin/settings', {
        settings: { active_server_providers: '' },
      }),
    )
    // The draft is consumed by the save: no Save left for the same selection
    await waitFor(() => expect(screen.queryByRole('button', { name: 'Save' })).not.toBeInTheDocument())
  })

  it('unchecking every visible provider saves the empty value even with an undiscovered stored id (#380)', async () => {
    // Saved value must stay "all active": appending the undiscovered id first
    // would turn it into a one-provider allowlist disabling the visible ones.
    get.mockResolvedValue(
      restricted('gemini,vertex-claude', { options: ['gemini', 'openai'] }) as never,
    )

    await openAiTab()
    fireEvent.click(screen.getByRole('checkbox', { name: 'gemini' }))

    expect(
      screen.getByText(/No providers selected — saving this activates ALL server providers/),
    ).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() =>
      expect(put).toHaveBeenCalledWith('/api/admin/settings', {
        settings: { active_server_providers: '' },
      }),
    )
  })

  it('saves the visible subset plus undiscovered stored ids (#380)', async () => {
    // A real subset is the only case where undiscovered stored ids ride along:
    // dropping them would silently narrow the restriction.
    get.mockResolvedValue(
      restricted('gemini,vertex-claude', { options: ['gemini', 'openai'] }) as never,
    )

    await openAiTab()
    fireEvent.click(screen.getByRole('checkbox', { name: 'openai' }))
    fireEvent.click(screen.getByRole('checkbox', { name: 'gemini' }))
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))

    await waitFor(() =>
      expect(put).toHaveBeenCalledWith('/api/admin/settings', {
        settings: { active_server_providers: 'openai,vertex-claude' },
      }),
    )
  })

  it('unchecking everything saves the empty value (empty = all, never "none")', async () => {
    await openAiTab()

    for (const provider of ['gemini', 'openai', 'vertex-claude']) {
      fireEvent.click(screen.getByRole('checkbox', { name: provider }))
    }
    expect(
      screen.getByText(/No providers selected — saving this activates ALL server providers/),
    ).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() =>
      expect(put).toHaveBeenCalledWith('/api/admin/settings', {
        settings: { active_server_providers: '' },
      }),
    )
  })

  it('Cancel restores the stored value and fires no save', async () => {
    get.mockResolvedValue(restricted('gemini') as never)

    await openAiTab()
    fireEvent.click(screen.getByRole('checkbox', { name: 'openai' }))
    expect(screen.getByRole('checkbox', { name: 'openai' })).toBeChecked()

    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(screen.getByRole('checkbox', { name: 'gemini' })).toBeChecked()
    expect(screen.getByRole('checkbox', { name: 'openai' })).not.toBeChecked()
    expect(screen.queryByRole('button', { name: 'Save' })).not.toBeInTheDocument()
    expect(put).not.toHaveBeenCalled()
  })

  it('hides Save and Cancel once the draft matches the stored value again', async () => {
    get.mockResolvedValue(restricted('gemini') as never)

    await openAiTab()
    expect(screen.queryByRole('button', { name: 'Save' })).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole('checkbox', { name: 'openai' }))
    expect(screen.getByRole('button', { name: 'Save' })).toBeInTheDocument()

    // Toggling back to the stored selection clears the dirty state
    fireEvent.click(screen.getByRole('checkbox', { name: 'openai' }))
    expect(screen.queryByRole('button', { name: 'Save' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Cancel' })).not.toBeInTheDocument()
  })

  it('matches saved provider IDs case-insensitively and saves canonical spelling', async () => {
    get.mockResolvedValue(restricted('Gemini,OpenAI') as never)

    await openAiTab()
    expect(screen.getByRole('checkbox', { name: 'gemini' })).toBeChecked()
    expect(screen.getByRole('checkbox', { name: 'openai' })).toBeChecked()
    expect(screen.getByRole('checkbox', { name: 'vertex-claude' })).not.toBeChecked()

    fireEvent.click(screen.getByRole('checkbox', { name: 'openai' }))
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() =>
      expect(put).toHaveBeenCalledWith('/api/admin/settings', {
        settings: { active_server_providers: 'gemini' },
      }),
    )
  })

  it('drops the unsaved draft when the stored value is reset, so Save cannot restore it', async () => {
    const settings = restricted('gemini', { source: 'db' })
    let reset = false
    get.mockImplementation(async () => (reset ? baseSettings() : settings))
    const del = vi.mocked(api.delete)
    del.mockImplementation(async () => {
      reset = true
      return {}
    })

    await openAiTab()
    fireEvent.click(screen.getByRole('checkbox', { name: 'openai' }))
    expect(screen.getByRole('button', { name: 'Save' })).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Reset' }))
    await waitFor(() =>
      expect(del).toHaveBeenCalledWith('/api/admin/settings/active_server_providers'),
    )
    // Draft dropped: the row renders the refetched (unrestricted) value, not the
    // pre-reset selection, and there is no stale Save left to click
    await waitFor(() => expect(screen.queryByRole('button', { name: 'Save' })).not.toBeInTheDocument())
    for (const box of providerBoxes()) expect(box).toBeChecked()
  })

  it('shows updated_by and the history toggle for a db-sourced value', async () => {
    get.mockResolvedValue(restricted('gemini', { source: 'db', updated_by: 'admin' }) as never)

    await openAiTab()
    expect(screen.getByText('Modified by admin')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Show history' })).toBeInTheDocument()
  })

  it('shows a hint when no server providers are available', async () => {
    get.mockResolvedValue(restricted('', { options: [] }) as never)

    await openAiTab()
    expect(screen.getByText('No server providers available')).toBeInTheDocument()
    expect(screen.queryByRole('checkbox')).not.toBeInTheDocument()
  })

  it('renders the description served by the settings API with the picker', async () => {
    await openAiTab()
    // No hardcoded copy in the page: the backend description is the single source.
    expect(screen.getByText('Description for active_server_providers')).toBeInTheDocument()
  })

  it('keeps the stored value read-only with a reset when discovery is unavailable', async () => {
    get.mockResolvedValue(
      restricted('gemini', { options: [], source: 'db', updated_by: 'admin' }) as never,
    )

    await openAiTab()
    expect(screen.getByText('Restricted to:')).toBeInTheDocument()
    expect(screen.getByText(/Provider discovery is currently unavailable/)).toBeInTheDocument()
    expect(screen.queryByRole('checkbox')).not.toBeInTheDocument()

    const del = vi.mocked(api.delete)
    del.mockResolvedValue({} as never)
    fireEvent.click(screen.getByRole('button', { name: 'Reset' }))
    await waitFor(() =>
      expect(del).toHaveBeenCalledWith('/api/admin/settings/active_server_providers'),
    )
    // Silent refetch after reset
    await waitFor(() => expect(get).toHaveBeenCalledTimes(2))
  })
})

describe('ServerSettingsPage AI model field width (#372)', () => {
  it('lets the AI model combobox grow instead of capping it at max-w-md', async () => {
    renderPage()
    fireEvent.click(await screen.findByRole('tab', { name: 'AI' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Configure model' }))

    const combobox = await screen.findByTestId('model-combobox')
    expect(combobox).toHaveClass('flex-1', 'min-w-0', 'max-w-3xl')
    expect(combobox).not.toHaveClass('max-w-md')
  })
})

describe('ServerSettingsPage save refetch gating', () => {
  it('keeps controls disabled until the post-save refetch resolves', async () => {
    renderPage()
    fireEvent.click(await screen.findByRole('tab', { name: 'Jenkins' }))
    fireEvent.click(screen.getByRole('button', { name: 'Edit JENKINS_USER' }))

    // The post-save silent refetch stays pending until we resolve it
    let resolveRefetch!: (value: TestSetting[]) => void
    const refetch = new Promise<TestSetting[]>((r) => {
      resolveRefetch = r
    })
    get.mockReturnValueOnce(refetch as never)

    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(put).toHaveBeenCalledTimes(1))

    // Save succeeded but the refetch is in flight: the editor stays disabled
    const savingButton = await screen.findByRole('button', { name: /saving/i })
    expect(savingButton).toBeDisabled()

    resolveRefetch(baseSettings())
    await waitFor(() =>
      expect(screen.queryByRole('button', { name: /saving/i })).toBeNull(),
    )
    expect(shown('jenkins_user')).toBe(true)
  })
})

describe('ServerSettingsPage revealed value cache (#343)', () => {
  // get mock serving the settings list, and decrypted values for reveal_key calls.
  // The returned `revealed` map is mutable so a test can change what the next
  // reveal returns (e.g. after a save changed the stored value).
  function mockGet(opts: { revealed?: Record<string, string>; base?: TestSetting[] } = {}) {
    const base = opts.base ?? baseSettings()
    const revealed = opts.revealed ?? {}
    get.mockImplementation(async (url: unknown) => {
      const key = typeof url === 'string' ? url.match(/reveal_key=([^&]+)/)?.[1] : undefined
      if (key) {
        const settings = baseSettings()
        const item = settings.find((s) => s.key === decodeURIComponent(key))
        if (item) item.value = revealed[key] ?? ''
        return settings
      }
      return base
    })
    return revealed
  }

  // The marker is the key of a row that only that tab renders (the env var is
  // a tooltip now, E2E-F27).
  async function openTab(name: string, markerKey: string) {
    renderPage()
    fireEvent.click(await screen.findByRole('tab', { name }))
    await waitForRow(markerKey)
  }

  it('clears the cached reveal on save — row shows the fresh value without a remount, and a new reveal shows the new value', async () => {
    const revealed = mockGet({ revealed: { jenkins_password: 'decrypted-old' } })
    await openTab('Jenkins', 'jenkins_password')

    // Reveal → row shows the decrypted value
    fireEvent.click(screen.getByRole('button', { name: 'Reveal value' }))
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Edit JENKINS_PASSWORD' }).textContent).toBe('decrypted-old'),
    )

    // Save a new value
    fireEvent.click(screen.getByRole('button', { name: 'Edit JENKINS_PASSWORD' }))
    fireEvent.change(screen.getByLabelText('Edit JENKINS_PASSWORD'), {
      target: { value: 'new-pass' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() =>
      expect(put).toHaveBeenCalledWith('/api/admin/settings', {
        settings: { jenkins_password: 'new-pass' }, // pragma: allowlist secret
      }),
    )

    // Silent refetch + cleared reveal: the row falls back to the fresh masked value
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Edit JENKINS_PASSWORD' }).textContent).toBe('••••••••'),
    )
    expect(screen.queryByText('decrypted-old')).not.toBeInTheDocument()
    // Row stayed mounted (no skeleton) and is masked again
    expect(document.querySelector('.animate-pulse')).toBeNull()
    expect(screen.getByRole('button', { name: 'Reveal value' })).toBeInTheDocument()

    // Revealing again fetches the NEW decrypted value
    revealed.jenkins_password = 'decrypted-new' // pragma: allowlist secret
    fireEvent.click(screen.getByRole('button', { name: 'Reveal value' }))
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Edit JENKINS_PASSWORD' }).textContent).toBe('decrypted-new'),
    )
  })

  it('drops a reveal response that arrives after a save cleared the cache', async () => {
    // The reveal GET stays pending until the test resolves it — after the save
    // has already refetched and cleared the cached reveal.
    let resolveReveal!: (value: TestSetting[]) => void
    get.mockImplementation(async (url: unknown) => {
      const key = typeof url === 'string' ? url.match(/reveal_key=([^&]+)/)?.[1] : undefined
      if (key) return new Promise<TestSetting[]>((r) => { resolveReveal = r })
      return baseSettings()
    })
    await openTab('Jenkins', 'jenkins_password')

    // Start the reveal — its response is deferred
    fireEvent.click(screen.getByRole('button', { name: 'Reveal value' }))

    // Save a new value while the reveal is still in flight
    fireEvent.click(screen.getByRole('button', { name: 'Edit JENKINS_PASSWORD' }))
    fireEvent.change(screen.getByLabelText('Edit JENKINS_PASSWORD'), {
      target: { value: 'new-pass' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(put).toHaveBeenCalledTimes(1))

    // Save completed: the row shows the fresh masked value
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Edit JENKINS_PASSWORD' }).textContent).toBe('••••••••'),
    )

    // The stale reveal response finally arrives with the OLD secret
    const stale = baseSettings()
    stale.find((s) => s.key === 'jenkins_password')!.value = 'decrypted-old' // pragma: allowlist secret
    resolveReveal(stale)

    // The late response must not resurrect the old secret — the row stays masked
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Edit JENKINS_PASSWORD' }).textContent).toBe('••••••••'),
    )
    expect(screen.queryByText('decrypted-old')).not.toBeInTheDocument()
  })

  it('clears the cached reveal on reset', async () => {
    const settings = baseSettings()
    const pw = settings.find((s) => s.key === 'jenkins_password')!
    pw.source = 'db'
    mockGet({ revealed: { jenkins_password: 'decrypted-old' }, base: settings }) // pragma: allowlist secret
    await openTab('Jenkins', 'jenkins_password')

    fireEvent.click(screen.getByRole('button', { name: 'Reveal value' }))
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Edit JENKINS_PASSWORD' }).textContent).toBe('decrypted-old'),
    )

    // Reset via the editor (the reset affordance lives in edit mode)
    fireEvent.click(screen.getByRole('button', { name: 'Edit JENKINS_PASSWORD' }))
    const del = vi.mocked(api.delete)
    del.mockResolvedValue({} as never)
    fireEvent.click(screen.getByRole('button', { name: 'Reset to default' }))
    await waitFor(() => expect(del).toHaveBeenCalledWith('/api/admin/settings/jenkins_password'))

    // The generic row stays in edit mode after a reset — close it to see the display value
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))

    // Fresh refetched value wins over the stale cached reveal
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Edit JENKINS_PASSWORD' }).textContent).toBe('••••••••'),
    )
    expect(screen.queryByText('decrypted-old')).not.toBeInTheDocument()
  })

  it('keeps reveals of other keys when saving an unrelated setting', async () => {
    // Explicit basic mode so only one sensitive Jira field (jira_api_token) renders
    const base = baseSettings()
    base.find((s) => s.key === 'jira_auth_mode')!.value = 'basic'
    mockGet({ revealed: { jira_api_token: 'decrypted-jira' }, base })
    await openTab('Jira', 'jira_api_token')

    fireEvent.click(screen.getByRole('button', { name: 'Reveal value' }))
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Edit JIRA_API_TOKEN' }).textContent).toBe('decrypted-jira'),
    )

    // Save jenkins_password — an unrelated key — on another tab
    fireEvent.click(screen.getByRole('tab', { name: 'Jenkins' }))
    fireEvent.click(screen.getByRole('button', { name: 'Edit JENKINS_PASSWORD' }))
    fireEvent.change(screen.getByLabelText('Edit JENKINS_PASSWORD'), {
      target: { value: 'new-pass' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(put).toHaveBeenCalledTimes(1))

    // The jira_api_token reveal survives: only the saved key was cleared
    fireEvent.click(screen.getByRole('tab', { name: 'Jira' }))
    expect(screen.getByRole('button', { name: 'Edit JIRA_API_TOKEN' }).textContent).toBe('decrypted-jira')
    expect(screen.getByRole('button', { name: 'Hide value' })).toBeInTheDocument()
  })
})

// E2E-F31 — an empty secret must not pretend to hold a value.
describe('ServerSettingsPage empty secret (#379)', () => {
  const SECRET = 'https://hooks.example.com/services/secret' // pragma: allowlist secret
  const editSlack = () => screen.getByRole('button', { name: 'Edit SLACK_WEBHOOK_URL' })

  async function slackRow() {
    fireEvent.click(await screen.findByRole('tab', { name: 'Notifications' }))
    const found = await waitFor(() => {
      const element = row('slack_webhook_url')
      expect(element).not.toBeNull()
      return element as HTMLElement
    })
    return found
  }

  it('renders nothing stored as empty — no dots, no reveal control', async () => {
    get.mockResolvedValue([
      mk('slack_webhook_url', 'Notifications', '', { sensitive: true, is_set: false }),
    ] as never)
    renderPage()

    const slack = await slackRow()
    expect(within(slack).getByRole('button', { name: 'Edit SLACK_WEBHOOK_URL' })).toHaveTextContent(
      '(empty)',
    )
    expect(within(slack).queryByRole('button', { name: 'Reveal value' })).toBeNull()
    expect(slack).not.toHaveTextContent('••••••••')
  })

  it('shows dots and a working reveal when a value is stored', async () => {
    const settings = [
      mk('slack_webhook_url', 'Notifications', '••••••••', { sensitive: true, is_set: true }),
    ]
    get.mockImplementation(async (url: unknown) => {
      if (typeof url === 'string' && url.includes('reveal_key=')) {
        return [mk('slack_webhook_url', 'Notifications', SECRET, { sensitive: true, is_set: true })] as never
      }
      return settings as never
    })
    renderPage()

    const slack = await slackRow()
    expect(editSlack().textContent).toBe('••••••••')
    fireEvent.click(within(slack).getByRole('button', { name: 'Reveal value' }))
    await waitFor(() => expect(editSlack().textContent).toBe(SECRET))
  })

  it('switches to dots and an eye after saving, and back to empty on reset', async () => {
    const settings = [
      mk('slack_webhook_url', 'Notifications', '', { sensitive: true, is_set: false, source: 'db' }),
    ]
    // A stored secret is masked on the next read, like the real API.
    get.mockImplementation(async (url: unknown) => {
      if (typeof url === 'string' && url.includes('reveal_key=')) {
        return settings.map(s => ({ ...s, value: s.is_set ? SECRET : '' })) as never
      }
      return settings as never
    })
    put.mockImplementation(async (_url: string, body?: unknown) => {
      const updates = (body as { settings?: Record<string, string> }).settings ?? {}
      for (const [key, value] of Object.entries(updates)) {
        Object.assign(settings.find(s => s.key === key)!, {
          value: value ? '••••••••' : '',
          is_set: Boolean(value),
          source: 'db',
        })
      }
      return {} as never
    })
    vi.mocked(api.delete).mockImplementation(async () => {
      Object.assign(settings[0], { value: '', is_set: false, source: 'db' })
      return {} as never
    })
    renderPage()

    await slackRow()
    expect(editSlack()).toHaveTextContent('(empty)')
    expect(screen.queryByRole('button', { name: 'Reveal value' })).toBeNull()

    fireEvent.click(editSlack())
    fireEvent.change(screen.getByLabelText('Edit SLACK_WEBHOOK_URL'), { target: { value: SECRET } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(editSlack().textContent).toBe('••••••••'))
    expect(screen.getByRole('button', { name: 'Reveal value' })).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Reveal value' }))
    await waitFor(() => expect(editSlack().textContent).toBe(SECRET))

    // Reset → nothing stored again, so the empty treatment comes back.
    fireEvent.click(editSlack())
    fireEvent.click(screen.getByRole('button', { name: 'Reset to default' }))
    await waitFor(() => expect(api.delete).toHaveBeenCalledWith('/api/admin/settings/slack_webhook_url'))
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(editSlack().textContent).toBe('(empty)'))
    expect(screen.queryByRole('button', { name: 'Reveal value' })).toBeNull()
  })
})

describe('ServerSettingsPage Jira field visibility', () => {
  it('finds auth-method-hidden Jira fields in search results', async () => {
    mockJiraModeSettings('basic')
    renderPage()
    await waitForRow('public_base_url')

    // jira_pat is hidden while Email+token is the selected method
    fireEvent.click(screen.getByRole('tab', { name: 'Jira' }))
    await waitForRow('jira_project_key')
    expect(shown('jira_pat')).toBe(false)

    // Search shows every matching Jira field regardless of method hiding
    fireEvent.change(screen.getByLabelText('Search settings'), { target: { value: 'jira_pat' } })
    await waitForRow('jira_pat')
    // Header count matches the visible rows
    expect(screen.getByText('(1 setting)')).toBeInTheDocument()
  })

  it('cancels an open edit on a field hidden by the newly selected method', async () => {
    mockJiraModeSettings('basic')
    renderPage()
    fireEvent.click(await screen.findByRole('tab', { name: 'Jira' }))
    await waitForRow('jira_project_key')

    // Open an edit on a basic-method field
    fireEvent.click(screen.getByRole('button', { name: 'Edit JIRA_EMAIL' }))
    const testButton = screen.getByRole('button', { name: /test connection/i })
    expect(testButton).toBeDisabled()

    // Switching to PAT hides JIRA_EMAIL — the edit is canceled, not stranded
    chooseJiraMethod('PAT (Bearer)')
    fireEvent.click(within(await screen.findByRole('alertdialog')).getByRole('button', { name: 'Switch' }))
    await waitFor(() => expect(testButton).toBeEnabled())
    expect(screen.queryByLabelText('Edit JIRA_EMAIL')).not.toBeInTheDocument()
    expect(shown('jira_email')).toBe(false)
    expect(shown('jira_pat')).toBe(true)
  })

  it('keeps an open editor and its draft when switching to a method that still shows the field', async () => {
    mockJiraModeSettings('basic')
    renderPage()
    fireEvent.click(await screen.findByRole('tab', { name: 'Jira' }))
    await waitForRow('jira_email')

    // Draft an edit on a method-independent field (visible under every method)
    fireEvent.click(screen.getByRole('button', { name: 'Edit JIRA_SITE_URL' }))
    fireEvent.change(screen.getByLabelText('Edit JIRA_SITE_URL'), {
      target: { value: 'https://draft.example.com' },
    })

    // Switch basic → PAT (verified method: confirmation dialog, then save)
    chooseJiraMethod('PAT (Bearer)')
    fireEvent.click(within(await screen.findByRole('alertdialog')).getByRole('button', { name: 'Switch' }))
    await waitFor(() =>
      expect(put).toHaveBeenCalledWith('/api/admin/settings', { settings: { jira_auth_mode: 'pat' } }),
    )
    await waitFor(() => expect(jiraAuthSelect()).toHaveTextContent('PAT (Bearer)'))

    // jira_site_url stays visible under PAT: the editor survives the switch with its draft
    expect(screen.getByLabelText('Edit JIRA_SITE_URL')).toHaveValue('https://draft.example.com')
    expect(shown('jira_pat')).toBe(true)
    expect(shown('jira_email')).toBe(false)
  })
})

describe('ServerSettingsPage env parity (#347)', () => {
  function paritySettings(): TestSetting[] {
    return [
      ...baseSettings(),
      // Restart-tagged settings promoted from _SERVER_ONLY_SETTINGS
      mk('default_user_role', 'Auth & Security', 'user', { restart_required: true }),
      mk('session_ttl_hours', 'Auth & Security', '24', { type: 'integer' }),
      // Sidecar settings (restart-required) in their own category
      mk('sidecar_host', 'Sidecar', '127.0.0.1', { restart_required: true }),
      mk('sidecar_port', 'Sidecar', '8087', { type: 'integer', restart_required: true }),
      mk('sidecar_url', 'Sidecar', '', { restart_required: true }),
      mk('acpx_agents', 'Sidecar', '', { restart_required: true }),
      mk('cli_agents', 'Sidecar', '', { restart_required: true }),
      mk('pi_sidecar_log_level', 'Sidecar', 'info', { restart_required: true }),
      mk('node_binary', 'Sidecar', '', { restart_required: true }),
      mk('pi_sidecar_transcripts_dir', 'Sidecar', '', { restart_required: true }),
      // Promoted Notifications settings
      mk('smtp_host', 'Notifications', 'smtp.example.com'),
      mk('smtp_port', 'Notifications', '587', { type: 'integer' }),
      mk('smtp_user', 'Notifications', 'bot'),
      mk('smtp_password', 'Notifications', 'secret', { sensitive: true }),
      mk('smtp_from', 'Notifications', 'rootcoz@example.com'),
      mk('slack_webhook_url', 'Notifications', 'https://hooks', { sensitive: true }),
      mk('alert_email_to', 'Notifications', ''),
    ]
  }

  it('renders the Sidecar and Notifications categories as tabs with their rows', async () => {
    get.mockResolvedValue(paritySettings() as never)
    renderPage()
    await waitForRow('public_base_url')

    const tabs = screen.getAllByRole('tab').map((t) => t.textContent)
    expect(tabs).toContain('Sidecar')
    expect(tabs).toContain('Notifications')

    fireEvent.click(screen.getByRole('tab', { name: 'Sidecar' }))
    await waitForRow('sidecar_host')
    expect(shown('sidecar_port')).toBe(true)
    expect(shown('pi_sidecar_log_level')).toBe(true)

    fireEvent.click(screen.getByRole('tab', { name: 'Notifications' }))
    await waitForRow('smtp_host')
    expect(shown('alert_email_to')).toBe(true)
  })

  it('shows restart badges on every restart-tagged setting row', async () => {
    get.mockResolvedValue(paritySettings() as never)
    renderPage()
    await waitForRow('public_base_url')

    // Sidecar tab: all 8 sidecar settings are restart-tagged
    fireEvent.click(screen.getByRole('tab', { name: 'Sidecar' }))
    await waitForRow('sidecar_host')
    expect(screen.getAllByLabelText('Restart required')).toHaveLength(8)

    // Auth & Security: default_user_role carries the badge too
    fireEvent.click(screen.getByRole('tab', { name: 'Auth & Security' }))
    await waitForRow('default_user_role')
    expect(screen.getAllByLabelText('Restart required')).toHaveLength(1)
  })

  it('renders sensitive Notification rows masked with a reveal affordance', async () => {
    get.mockResolvedValue(paritySettings() as never)
    renderPage()
    fireEvent.click(await screen.findByRole('tab', { name: 'Notifications' }))
    await waitForRow('smtp_host')

    expect(screen.getAllByText('••••••••').length).toBeGreaterThanOrEqual(2)
    expect(screen.getAllByRole('button', { name: 'Reveal value' }).length).toBeGreaterThanOrEqual(2)
  })

  it('saves a promoted setting row (session TTL) through the normal flow', async () => {
    get.mockResolvedValue(paritySettings() as never)
    renderPage()
    fireEvent.click(await screen.findByRole('tab', { name: 'Auth & Security' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Edit SESSION_TTL_HOURS' }))

    fireEvent.change(screen.getByLabelText('Edit SESSION_TTL_HOURS'), { target: { value: '48' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() =>
      expect(put).toHaveBeenCalledWith('/api/admin/settings', {
        settings: { session_ttl_hours: '48' },
      }),
    )
  })

  it('saves the lockout threshold as typed, with the 0-disables hint', async () => {
    get.mockResolvedValue([
      mk('login_lockout_threshold', 'Auth & Security', '5', {
        type: 'integer',
        description: 'Consecutive failed logins that lock an account on this server.',
      }),
    ] as never)
    renderPage()
    fireEvent.click(await screen.findByRole('tab', { name: 'Auth & Security' }))
    await waitForRow('login_lockout_threshold')
    expect(
      screen.getByText('Failed logins before the account locks. 0 disables lockout.'),
    ).toBeInTheDocument()

    // 0 means "lockout off" — a real value, saved exactly as typed (no clamping)
    fireEvent.click(screen.getByRole('button', { name: 'Edit LOGIN_LOCKOUT_THRESHOLD' }))
    fireEvent.change(screen.getByLabelText('Edit LOGIN_LOCKOUT_THRESHOLD'), { target: { value: '0' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() =>
      expect(put).toHaveBeenCalledWith('/api/admin/settings', {
        settings: { login_lockout_threshold: '0' },
      }),
    )

    fireEvent.click(await screen.findByRole('button', { name: 'Edit LOGIN_LOCKOUT_THRESHOLD' }))
    fireEvent.change(screen.getByLabelText('Edit LOGIN_LOCKOUT_THRESHOLD'), { target: { value: '7' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() =>
      expect(put).toHaveBeenCalledWith('/api/admin/settings', {
        settings: { login_lockout_threshold: '7' },
      }),
    )
  })

  it('shows the trust warning from the save response for a non-loopback sidecar host', async () => {
    get.mockResolvedValue(paritySettings() as never)
    put.mockResolvedValueOnce({
      updated: ['sidecar_host'],
      warnings: ['sidecar_host is not a loopback address — only bind a host you fully trust.'],
    } as never)

    renderPage()
    fireEvent.click(await screen.findByRole('tab', { name: 'Sidecar' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Edit SIDECAR_HOST' }))

    fireEvent.change(screen.getByLabelText('Edit SIDECAR_HOST'), { target: { value: '0.0.0.0' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    expect(await screen.findByRole('status')).toHaveTextContent(/only bind a host you fully trust/)

    // The warning clears on the next save
    fireEvent.click(screen.getByRole('button', { name: 'Edit SIDECAR_HOST' }))
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(screen.queryByRole('status')).toBeNull())
  })

  it('no longer renders the standalone Free AI models card in the AI tab', async () => {
    renderPage()
    fireEvent.click(await screen.findByRole('tab', { name: 'AI' }))
    await waitForRow('active_server_providers')
    expect(screen.queryByText(/Free AI models/)).not.toBeInTheDocument()
  })
})

// E2E-F34 — lockout is a switch plus a number, not a magic threshold of 0.
describe('ServerSettingsPage login lockout toggle (#379 E2E-F34)', () => {
  function lockoutSettings(enabled = 'true') {
    const settings = [
      mk('login_lockout_enabled', 'Auth & Security', enabled, {
        type: 'boolean',
        label: 'Enable login lockout',
      }),
      mk('login_lockout_threshold', 'Auth & Security', '5', {
        type: 'integer',
        label: 'Failed logins before lockout',
      }),
    ]
    get.mockImplementation(async () => settings as never)
    put.mockImplementation(async (_url: string, body?: unknown) => {
      const updates = (body as { settings?: Record<string, string> }).settings ?? {}
      for (const [key, value] of Object.entries(updates)) {
        const setting = settings.find((s) => s.key === key)
        if (setting) Object.assign(setting, { value, source: 'db' })
      }
      return {} as never
    })
    return settings
  }

  const lockoutToggle = () => screen.getByRole('switch', { name: 'Toggle LOGIN_LOCKOUT_ENABLED' })
  const thresholdEdit = () => screen.getByRole('button', { name: 'Edit LOGIN_LOCKOUT_THRESHOLD' })

  async function openLockout() {
    renderPage()
    fireEvent.click(await screen.findByRole('tab', { name: 'Auth & Security' }))
    await waitForRow('login_lockout_threshold')
  }

  it('renders the switch with the stored value and keeps the threshold editable while it is on', async () => {
    lockoutSettings('true')
    await openLockout()

    // Booleans are switches on this page — this one included.
    expect(lockoutToggle()).toBeChecked()
    expect(thresholdEdit()).toBeEnabled()
    expect(screen.queryByText(/Lockout is disabled/)).toBeNull()
  })

  it('turning lockout off disables the threshold input with the reason, and turning it on re-enables it without losing the number', async () => {
    lockoutSettings('true')
    await openLockout()

    // Draft a number first: the toggle must disable the input, not drop the draft.
    fireEvent.click(thresholdEdit())
    const input = screen.getByLabelText('Edit LOGIN_LOCKOUT_THRESHOLD')
    fireEvent.change(input, { target: { value: '7' } })

    fireEvent.click(lockoutToggle())
    await waitFor(() =>
      expect(put).toHaveBeenCalledWith('/api/admin/settings', {
        settings: { login_lockout_enabled: 'false' },
      }),
    )
    await waitFor(() => expect(input).toBeDisabled())
    expect(input).toHaveValue(7)
    expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled()
    expect(screen.getByText(/Lockout is disabled/)).toBeInTheDocument()

    fireEvent.click(lockoutToggle())
    await waitFor(() =>
      expect(put).toHaveBeenLastCalledWith('/api/admin/settings', {
        settings: { login_lockout_enabled: 'true' },
      }),
    )
    await waitFor(() => expect(input).toBeEnabled())
    // The number the admin typed is still there — nothing was reset to 0.
    expect(input).toHaveValue(7)
    expect(screen.queryByText(/Lockout is disabled/)).toBeNull()
  })

  it('keeps the stored threshold visible and uneditable while lockout is off', async () => {
    lockoutSettings('false')
    await openLockout()

    expect(lockoutToggle()).not.toBeChecked()
    expect(thresholdEdit()).toBeDisabled()
    expect(thresholdEdit()).toHaveTextContent('5')
    expect(screen.getByText(/Lockout is disabled/)).toBeInTheDocument()
  })

  it('never seeds the threshold row from itself: an absent switch leaves it editable', async () => {
    // Older servers omit login_lockout_enabled — no switch, no lock-out gate.
    get.mockResolvedValue([
      mk('login_lockout_threshold', 'Auth & Security', '5', { type: 'integer' }),
    ] as never)
    await openLockout()

    expect(screen.queryByRole('switch')).toBeNull()
    expect(thresholdEdit()).toBeEnabled()
  })
})

// E2E-F33 — no setting appears twice: one control, carrying its own affordances.
describe('ServerSettingsPage one control per setting (#379 E2E-F33)', () => {
  it('edits jira_auth_mode through the card selector only, with its label, source, reset and history in the card', async () => {
    const settings = baseSettings()
    Object.assign(settings.find((s) => s.key === 'jira_auth_mode')!, {
      value: 'pat',
      label: 'Jira auth method',
      is_set: true,
      source: 'db',
      updated_by: 'admin',
      updated_at: '2025-01-01 00:00:00',
    })
    get.mockResolvedValue(settings as never)
    renderPage()
    fireEvent.click(await screen.findByRole('tab', { name: 'Jira' }))
    await waitForRow('jira_project_key')

    // The selector is the single editor: no row, no "Managed via …" echo.
    expect(screen.getByRole('combobox', { name: 'Select Jira auth method' })).toHaveTextContent(
      'PAT (Bearer)',
    )
    expect(row('jira_auth_mode')).toBeNull()
    expect(screen.queryByText(/Managed via/)).toBeNull()

    // Everything the row used to carry now lives in the card.
    expect(screen.getByText('Jira auth method')).toBeInTheDocument()
    // ... including the env var, as a tooltip on the card header's label.
    expect(await envVarTooltip('Jira auth method')).toBe('JIRA_AUTH_MODE')
    expect(screen.getByText('db')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Show history' })).toBeInTheDocument()

    const del = vi.mocked(api.delete)
    del.mockResolvedValue({} as never)
    fireEvent.click(screen.getByRole('button', { name: 'Reset Jira auth method' }))
    await waitFor(() => expect(del).toHaveBeenCalledWith('/api/admin/settings/jira_auth_mode'))
  })
})

describe('ServerSettingsPage save env setting to DB', () => {
  function envSettings() {
    const settings = baseSettings()
    // jenkins_url from env, jenkins_user from db — only the env row gets the button
    const url = settings.find(s => s.key === 'jenkins_url')!
    url.source = 'env'
    settings.find(s => s.key === 'jenkins_user')!.source = 'db'
    return settings
  }

  async function renderJenkinsRows() {
    renderPage()
    fireEvent.change(await screen.findByLabelText('Search settings'), { target: { value: 'jenkins_' } })
    await waitForRow('jenkins_user')
  }

  it('renders Save to DB only on env-sourced rows', async () => {
    get.mockResolvedValue(envSettings() as never)
    await renderJenkinsRows()

    expect(shown('jenkins_url')).toBe(true)
    expect(screen.getAllByRole('button', { name: 'Save to DB' })).toHaveLength(1)
  })

  it('never offers Save to DB for server-only settings, even when env-sourced', async () => {
    const settings = baseSettings()
    const adminKey = settings.find(s => s.key === 'admin_key')!
    adminKey.source = 'env'
    settings.push(
      mk('db_path', 'Server', '/var/lib/rootcoz/db.sqlite', { source: 'env' }),
      mk('debug', 'Server', 'true', { type: 'boolean', source: 'env' }),
    )
    get.mockResolvedValue(settings as never)
    renderPage()

    // Server tab: db_path and debug are env-sourced but server-only
    fireEvent.click(await screen.findByRole('tab', { name: 'Server' }))
    await waitForRow('db_path')
    expect(shown('debug')).toBe(true)
    expect(screen.queryByRole('button', { name: 'Save to DB' })).not.toBeInTheDocument()

    // Auth & Security tab: admin_key likewise
    fireEvent.click(screen.getByRole('tab', { name: 'Auth & Security' }))
    await waitForRow('admin_key')
    expect(screen.queryByRole('button', { name: 'Save to DB' })).not.toBeInTheDocument()
  })

  it('never offers Save to DB for derived settings (persistable: false), even when env-sourced', async () => {
    const settings = baseSettings()
    settings.push(
      mk('sidecar_url', 'Sidecar', 'http://127.0.0.1:9100', {
        source: 'env',
        persistable: false,
      }),
    )
    get.mockResolvedValue(settings as never)
    renderPage()

    fireEvent.click(await screen.findByRole('tab', { name: 'Sidecar' }))
    await waitForRow('sidecar_url')
    expect(screen.queryByRole('button', { name: 'Save to DB' })).not.toBeInTheDocument()
  })

  it('persists the env value and refetches with source db', async () => {
    get.mockResolvedValueOnce(envSettings() as never)
    const after = envSettings()
    after.find(s => s.key === 'jenkins_url')!.source = 'db'
    get.mockResolvedValueOnce(after as never)
    await renderJenkinsRows()

    fireEvent.click(screen.getByRole('button', { name: 'Save to DB' }))
    await waitFor(() => expect(persistSetting).toHaveBeenCalledWith('jenkins_url'))
    await waitFor(() => expect(get).toHaveBeenCalledTimes(2))

    // Silent refetch rendered the db-sourced row: button gone, both badges now db
    await waitFor(() => expect(screen.queryByRole('button', { name: 'Save to DB' })).toBeNull())
    expect(screen.getAllByText('db')).toHaveLength(2)
    expect(screen.queryByText('env')).toBeNull()
  })

  it('surfaces the error banner when persist fails with 400', async () => {
    get.mockResolvedValue(envSettings() as never)
    const err = new ApiError(0, '', null)
    err.status = 400
    err.body = { detail: 'jenkins_url has no environment value to persist' }
    persistSetting.mockRejectedValueOnce(err as never)
    await renderJenkinsRows()

    fireEvent.click(screen.getByRole('button', { name: 'Save to DB' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('jenkins_url has no environment value to persist')
    // Row still shows the env source — no silent state flip
    expect(screen.getByRole('button', { name: 'Save to DB' })).toBeInTheDocument()
  })
})

describe('ServerSettingsPage export and import settings', () => {
  const exportDoc = {
    version: 1,
    exported_at: '2025-01-01T00:00:00+00:00',
    settings: { jenkins_url: 'https://jenkins.example.com' },
  }

  // jsdom has no Blob URL support — stub the object-URL lifecycle.
  let createObjectURL: ReturnType<typeof vi.fn>
  let revokeObjectURL: ReturnType<typeof vi.fn>
  beforeEach(() => {
    createObjectURL = vi.fn(() => 'blob:settings')
    revokeObjectURL = vi.fn()
    Object.defineProperty(URL, 'createObjectURL', { value: createObjectURL, configurable: true })
    Object.defineProperty(URL, 'revokeObjectURL', { value: revokeObjectURL, configurable: true })
  })

  it('exports DB settings as a JSON download without secrets by default', async () => {
    renderPage()
    await waitForRow('public_base_url')
    get.mockResolvedValueOnce(exportDoc as never)

    // Export opens the dialog; the secrets choice lives inside it.
    fireEvent.click(screen.getByRole('button', { name: /Export settings/ }))
    const dialog = await screen.findByRole('alertdialog')
    expect(dialog).toHaveTextContent('Export settings')
    expect(within(dialog).getByRole('checkbox', { name: 'Include secrets in export' })).not.toBeChecked()

    fireEvent.click(within(dialog).getByRole('button', { name: 'Export' }))
    await waitFor(() => expect(createObjectURL).toHaveBeenCalledTimes(1))
    expect(get).toHaveBeenLastCalledWith('/api/admin/settings/export')
    expect(revokeObjectURL).toHaveBeenCalledWith('blob:settings')
  })

  it('warns about plaintext when secrets are included in the export dialog', async () => {
    renderPage()
    await waitForRow('public_base_url')
    get.mockResolvedValueOnce(exportDoc as never)

    fireEvent.click(screen.getByRole('button', { name: /Export settings/ }))
    const dialog = await screen.findByRole('alertdialog')

    // Checking secrets inside the dialog surfaces the plaintext warning.
    fireEvent.click(within(dialog).getByRole('checkbox', { name: 'Include secrets in export' }))
    expect(dialog).toHaveTextContent('plaintext')

    fireEvent.click(within(dialog).getByRole('button', { name: 'Export' }))
    await waitFor(() => expect(createObjectURL).toHaveBeenCalledTimes(1))
    expect(get).toHaveBeenLastCalledWith('/api/admin/settings/export?include_sensitive=true')
  })

  it('keeps the toolbar free of orphaned controls: no global expand/collapse, no secrets checkbox', async () => {
    renderPage()
    await waitForRow('public_base_url')

    expect(screen.queryByRole('button', { name: 'Expand all categories' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Collapse all categories' })).not.toBeInTheDocument()
    expect(screen.queryByRole('checkbox', { name: 'Include secrets in export' })).not.toBeInTheDocument()
  })

  it('imports a file after confirmation and shows the diff summary', async () => {
    post.mockResolvedValue({
      applied: { jenkins_url: { from: null, to: 'https://jenkins.example.com' } },
      unchanged: [],
      restart_required: ['log_level'],
    } as never)
    renderPage()
    await waitForRow('public_base_url')

    const input = document.querySelector('input[type="file"]')!
    fireEvent.change(input, {
      target: { files: [new File([JSON.stringify(exportDoc)], 'settings.json', { type: 'application/json' })] },
    })
    const dialog = await screen.findByRole('alertdialog')
    expect(dialog).toHaveTextContent('settings.json')

    fireEvent.click(within(dialog).getByRole('button', { name: 'Import' }))
    expect(await screen.findByRole('status')).toHaveTextContent('Imported 1 setting, 0 unchanged')
    expect(post).toHaveBeenCalledWith('/api/admin/settings/import', exportDoc)
    expect(screen.getByText(/Restart required for: log_level/)).toBeInTheDocument()
    // The refetch after import re-renders settings (get called again)
    await waitFor(() => expect(get).toHaveBeenCalledTimes(2))
  })

  it('surfaces a validation error and keeps the dialog open for retry', async () => {
    const err = new ApiError(0, '', null)
    err.status = 422
    err.body = { detail: 'Invalid settings: jenkins_timeout: must be an integer' }
    post.mockRejectedValueOnce(err as never)
    renderPage()
    await waitForRow('public_base_url')

    const input = document.querySelector('input[type="file"]')!
    fireEvent.change(input, {
      target: { files: [new File([JSON.stringify(exportDoc)], 'settings.json', { type: 'application/json' })] },
    })
    const dialog = await screen.findByRole('alertdialog')
    fireEvent.click(within(dialog).getByRole('button', { name: 'Import' }))

    expect(await screen.findByRole('alert')).toHaveTextContent('jenkins_timeout: must be an integer')
    // The dialog stayed open — the parsed document is still available
    expect(screen.getByRole('alertdialog')).toHaveTextContent('settings.json')
  })

  it('retries the same file without reselection after a failed import', async () => {
    const err = new ApiError(0, '', null)
    err.status = 422
    err.body = { detail: 'Invalid settings: jenkins_timeout: must be an integer' }
    post.mockRejectedValueOnce(err as never)
    post.mockResolvedValueOnce({ applied: {}, unchanged: [], restart_required: [] } as never)
    renderPage()
    await waitForRow('public_base_url')

    const input = document.querySelector('input[type="file"]')!
    fireEvent.change(input, {
      target: { files: [new File([JSON.stringify(exportDoc)], 'settings.json', { type: 'application/json' })] },
    })
    await screen.findByRole('alertdialog')
    fireEvent.click(within(screen.getByRole('alertdialog')).getByRole('button', { name: 'Import' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('jenkins_timeout: must be an integer')

    // Retry the confirm without reselecting the file — now it succeeds
    fireEvent.click(within(screen.getByRole('alertdialog')).getByRole('button', { name: 'Import' }))
    expect(await screen.findByRole('status')).toHaveTextContent('Imported 0 settings, 0 unchanged')
    await waitFor(() => expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument())
    // The retry cleared the previous attempt's error — no stale alert beside
    // the success summary (#361 Qodo r2)
    expect(screen.queryByText(/must be an integer/)).not.toBeInTheDocument()
  })

  it('cancels an open editor and clears revealed values after a successful import', async () => {
    get.mockImplementation(async (url: unknown) => {
      const key = typeof url === 'string' ? url.match(/reveal_key=([^&]+)/)?.[1] : undefined
      const settings = baseSettings()
      if (key) {
        const item = settings.find((s) => s.key === decodeURIComponent(key))
        if (item) item.value = 'decrypted-old' // pragma: allowlist secret
      }
      return settings
    })
    post.mockResolvedValue({ applied: {}, unchanged: [], restart_required: [] } as never)
    renderPage()

    // Reveal a sensitive value, then open its editor with a stale draft
    fireEvent.click(await screen.findByRole('tab', { name: 'Jenkins' }))
    await waitForRow('jenkins_password')
    fireEvent.click(screen.getByRole('button', { name: 'Reveal value' }))
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Edit JENKINS_PASSWORD' }).textContent).toBe('decrypted-old'),
    )
    fireEvent.click(screen.getByRole('button', { name: 'Edit JENKINS_PASSWORD' }))
    expect(screen.getByLabelText('Edit JENKINS_PASSWORD')).toBeInTheDocument()

    // Import a document that touches the revealed key
    const importDoc = { version: 1, settings: { jenkins_password: 'imported' } } // pragma: allowlist secret
    const input = document.querySelector('input[type="file"]')!
    fireEvent.change(input, {
      target: { files: [new File([JSON.stringify(importDoc)], 'settings.json', { type: 'application/json' })] },
    })
    await screen.findByRole('alertdialog')
    fireEvent.click(within(screen.getByRole('alertdialog')).getByRole('button', { name: 'Import' }))
    expect(await screen.findByRole('status')).toHaveTextContent('Imported 0 settings, 0 unchanged')

    // The open editor was cancelled — no stale draft can overwrite the import
    expect(screen.queryByRole('textbox', { name: 'Edit JENKINS_PASSWORD' })).not.toBeInTheDocument()
    // The cached reveal was cleared — the row falls back to the masked value
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Edit JENKINS_PASSWORD' }).textContent).toBe('••••••••'),
    )
    expect(screen.queryByText('decrypted-old')).not.toBeInTheDocument()
  })

  it('keeps an open editor and its draft when the import does not touch that setting', async () => {
    post.mockResolvedValue({ applied: {}, unchanged: [], restart_required: [] } as never)
    renderPage()

    // Open an editor on a setting the import document will not contain
    fireEvent.click(await screen.findByRole('tab', { name: 'Jenkins' }))
    await waitForRow('jenkins_user')
    fireEvent.click(screen.getByRole('button', { name: 'Edit JENKINS_USER' }))
    fireEvent.change(screen.getByLabelText('Edit JENKINS_USER'), {
      target: { value: 'unsaved-draft' },
    })

    const importDoc = { version: 1, settings: { jenkins_password: 'imported' } } // pragma: allowlist secret
    const input = document.querySelector('input[type="file"]')!
    fireEvent.change(input, {
      target: { files: [new File([JSON.stringify(importDoc)], 'settings.json', { type: 'application/json' })] },
    })
    await screen.findByRole('alertdialog')
    fireEvent.click(within(screen.getByRole('alertdialog')).getByRole('button', { name: 'Import' }))
    expect(await screen.findByRole('status')).toHaveTextContent('Imported 0 settings, 0 unchanged')

    // The untouched editor keeps its draft — only imported keys cancel (#361 Qodo r2)
    expect(screen.getByLabelText('Edit JENKINS_USER')).toHaveValue('unsaved-draft')
  })

  it('rejects a non-JSON file before any request', async () => {
    renderPage()
    await waitForRow('public_base_url')

    const input = document.querySelector('input[type="file"]')!
    fireEvent.change(input, {
      target: { files: [new File(['not json'], 'settings.txt', { type: 'text/plain' })] },
    })
    expect(await screen.findByRole('alert')).toHaveTextContent('not a valid JSON file')
    expect(post).not.toHaveBeenCalled()
  })
})

// #379: every enumerated setting is a picker, so an admin never has to guess a
// value the server already knows — and the choices it fetches at runtime must
// neither block the page nor drop a value it does not list.
describe('ServerSettingsPage enumerated setting pickers', () => {
  const EXPORTERS = [
    { name: 'reportportal', display_name: 'Report Portal', enabled: true },
    { name: 'elastic', display_name: 'Elastic', enabled: true },
    { name: 'slack', display_name: 'Slack', enabled: false },
  ]
  const CATALOG = { providers: { claude: [], gemini: [], 'vertex-claude': [] } }
  const USERS = { users: [{ username: 'alice' }, { username: 'bob' }] }

  function enumSettings(overrides: Record<string, Partial<TestSetting>> = {}): TestSetting[] {
    const settings = baseSettings()
    settings.push(
      mk('default_user_role', 'Auth & Security', 'reviewer', {
        options: ['viewer', 'reviewer', 'operator'],
      }),
      mk('log_level', 'Server', 'INFO', {
        options: ['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'],
        restart_required: true,
      }),
      mk('auto_push_exporters', 'Server', '', {
        options: null,
        options_source: 'exporters',
        multiple: true,
      }),
      mk('ambient_auth_providers', 'AI', '', {
        options: null,
        options_source: 'providers',
        multiple: true,
      }),
      mk('allowed_users', 'Auth & Security', '', {
        options: null,
        options_source: 'users',
        multiple: true,
        allow_custom: true,
      }),
      mk('datadog_site', 'Datadog', 'datadoghq.com', {
        options: ['datadoghq.com', 'datadoghq.eu'],
        allow_custom: true,
      }),
    )
    for (const [key, props] of Object.entries(overrides)) {
      Object.assign(settings.find((s) => s.key === key)!, props)
    }
    return settings
  }

  /** Serve the settings plus the endpoints the dynamic lists come from. */
  function mockEnumSettings(
    overrides: Record<string, Partial<TestSetting>> = {},
    failed: string[] = [],
  ) {
    const settings = enumSettings(overrides)
    get.mockImplementation(async (url: unknown) => {
      const path = String(url)
      if (path === '/api/exporters') {
        if (failed.includes('exporters')) throw new Error('exporters unavailable')
        return EXPORTERS as never
      }
      if (path === '/api/ai-models') {
        if (failed.includes('providers')) throw new Error('catalog unavailable')
        return CATALOG as never
      }
      if (path === '/api/admin/users') {
        if (failed.includes('users')) throw new Error('users unavailable')
        return USERS as never
      }
      return settings as never
    })
    // Persist what the page saves, so a refetch shows the saved value.
    put.mockImplementation(async (_url: string, body?: unknown) => {
      const updates = (body as { settings?: Record<string, string> }).settings ?? {}
      for (const [key, value] of Object.entries(updates)) {
        const setting = settings.find((s) => s.key === key)
        if (setting) Object.assign(setting, { value, source: 'db' })
      }
      return {} as never
    })
  }

  it('renders the static settings as searchable pickers pre-selected with the stored value and saves the choice', async () => {
    mockEnumSettings()
    renderPage()

    // One control for every enumerated setting (E2E-F23): the same picker the
    // dynamic lists use, searchable even with three choices.
    const logLevel = await screen.findByRole('combobox', { name: 'Select LOG_LEVEL' })
    expect(logLevel).toHaveTextContent('INFO')
    fireEvent.click(logLevel)
    fireEvent.change(searchBox('LOG_LEVEL'), { target: { value: 'deb' } })
    expect(optionNames()).toEqual(['DEBUG'])
    fireEvent.keyDown(searchBox('LOG_LEVEL'), { key: 'Enter' })
    await waitFor(() =>
      expect(put).toHaveBeenCalledWith('/api/admin/settings', { settings: { log_level: 'DEBUG' } }),
    )
    await waitFor(() =>
      expect(screen.getByRole('combobox', { name: 'Select LOG_LEVEL' })).toHaveTextContent('DEBUG'),
    )

    fireEvent.click(screen.getByRole('tab', { name: 'Auth & Security' }))
    const role = await screen.findByRole('combobox', { name: 'Select DEFAULT_USER_ROLE' })
    expect(role).toHaveTextContent('reviewer')
    pickSingle('DEFAULT_USER_ROLE', 'operator')
    await waitFor(() =>
      expect(put).toHaveBeenCalledWith('/api/admin/settings', {
        settings: { default_user_role: 'operator' },
      }),
    )
    await waitFor(() =>
      expect(screen.getByRole('combobox', { name: 'Select DEFAULT_USER_ROLE' })).toHaveTextContent(
        'operator',
      ),
    )
  })

  it('lets a single-value picker type a custom value when the server allows it', async () => {
    // datadog_site is a published list with allow_custom: a newly released
    // region must be typable, not only pickable from the list.
    mockEnumSettings()
    renderPage()
    fireEvent.click(await screen.findByRole('tab', { name: 'Datadog' }))

    fireEvent.click(await screen.findByRole('combobox', { name: 'Select DATADOG_SITE' }))
    fireEvent.change(searchBox('DATADOG_SITE'), {
      target: { value: 'us5.datadoghq.com' },
    })
    // The typed value is offered as a custom row and saves what was typed.
    const custom = screen.getByRole('option', { name: 'Add "us5.datadoghq.com"' })
    fireEvent.mouseDown(custom)

    await waitFor(() =>
      expect(put).toHaveBeenCalledWith('/api/admin/settings', {
        settings: { datadog_site: 'us5.datadoghq.com' },
      }),
    )
  })

  it('does not offer a custom entry when the server forbids it', async () => {
    // allow_custom is the server's decision: a closed set must stay closed.
    mockEnumSettings()
    renderPage()

    fireEvent.click(await screen.findByRole('combobox', { name: 'Select LOG_LEVEL' }))
    fireEvent.change(searchBox('LOG_LEVEL'), { target: { value: 'LOUD' } })
    // Nothing matches and no custom row is offered: a closed set stays closed.
    expect(screen.queryAllByRole('option')).toEqual([])
  })

  it('cancels a single pick with Escape without saving', async () => {
    mockEnumSettings()
    renderPage()

    fireEvent.click(await screen.findByRole('combobox', { name: 'Select LOG_LEVEL' }))
    fireEvent.change(searchBox('LOG_LEVEL'), { target: { value: 'error' } })
    expect(optionNames()).toEqual(['ERROR'])
    fireEvent.keyDown(searchBox('LOG_LEVEL'), { key: 'Escape' })

    await waitFor(() => expect(screen.queryByRole('listbox')).toBeNull())
    expect(put).not.toHaveBeenCalled()
    expect(screen.getByRole('combobox', { name: 'Select LOG_LEVEL' })).toHaveTextContent('INFO')
  })

  it('searches exporters by subsequence, ranks prefix matches first, and saves the picks in order', async () => {
    mockEnumSettings()
    renderPage()
    await waitForRow('auto_push_exporters')

    openPicker('AUTO_PUSH_EXPORTERS')
    const search = searchBox('AUTO_PUSH_EXPORTERS')
    // Case-insensitive subsequence: r-p-t finds reportportal.
    fireEvent.change(search, { target: { value: 'RPT' } })
    expect(optionNames()).toEqual(['reportportal'])
    // Prefix matches come first, then matches that start later in the word.
    fireEvent.change(search, { target: { value: 'e' } })
    expect(optionNames()).toEqual(['elastic', 'reportportal'])

    fireEvent.change(search, { target: { value: 'report' } })
    fireEvent.keyDown(search, { key: 'Enter' })
    await waitFor(() =>
      expect(put).toHaveBeenLastCalledWith('/api/admin/settings', {
        settings: { auto_push_exporters: 'reportportal' },
      }),
    )
    // multiple: the menu stays open with a cleared query so the next pick starts fresh.
    expect(searchBox('AUTO_PUSH_EXPORTERS')).toHaveValue('')
    expect(screen.getByRole('listbox')).toBeInTheDocument()

    fireEvent.change(searchBox('AUTO_PUSH_EXPORTERS'), { target: { value: 'elas' } })
    fireEvent.keyDown(searchBox('AUTO_PUSH_EXPORTERS'), { key: 'Enter' })
    await waitFor(() =>
      expect(put).toHaveBeenLastCalledWith('/api/admin/settings', {
        settings: { auto_push_exporters: 'reportportal,elastic' },
      }),
    )
    expect(screen.getByRole('button', { name: 'Remove reportportal' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Remove elastic' })).toBeInTheDocument()
  })

  it('shows an unconfigured exporter disabled with its hint and closes on Escape', async () => {
    mockEnumSettings()
    renderPage()
    await waitForRow('auto_push_exporters')

    openPicker('AUTO_PUSH_EXPORTERS')
    const slack = screen.getByRole('option', { name: /slack/ })
    expect(slack).toHaveAttribute('aria-disabled', 'true')
    expect(within(slack).getByText('not configured')).toBeInTheDocument()
    // An exporter that would never run cannot be picked
    fireEvent.mouseDown(slack)
    expect(put).not.toHaveBeenCalled()

    fireEvent.keyDown(searchBox('AUTO_PUSH_EXPORTERS'), { key: 'Escape' })
    await waitFor(() => expect(screen.queryByRole('listbox')).toBeNull())
  })

  it('lists the provider catalog for the ambient auth multiselect and saves the selection', async () => {
    mockEnumSettings()
    renderPage()
    fireEvent.click(await screen.findByRole('tab', { name: 'AI' }))
    await waitForRow('ambient_auth_providers')

    openPicker('AMBIENT_AUTH_PROVIDERS')
    fireEvent.change(searchBox('AMBIENT_AUTH_PROVIDERS'), { target: { value: 'cla' } })
    fireEvent.keyDown(searchBox('AMBIENT_AUTH_PROVIDERS'), { key: 'Enter' })
    await waitFor(() =>
      expect(put).toHaveBeenLastCalledWith('/api/admin/settings', {
        settings: { ambient_auth_providers: 'claude' },
      }),
    )

    // Down/Up move the highlight; Enter takes it.
    fireEvent.change(searchBox('AMBIENT_AUTH_PROVIDERS'), { target: { value: 'gem' } })
    fireEvent.keyDown(searchBox('AMBIENT_AUTH_PROVIDERS'), { key: 'Enter' })
    await waitFor(() =>
      expect(put).toHaveBeenLastCalledWith('/api/admin/settings', {
        settings: { ambient_auth_providers: 'claude,gemini' },
      }),
    )
    expect(screen.getByRole('button', { name: 'Remove claude' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Remove gemini' })).toBeInTheDocument()
  })

  it('suggests existing users for the allow-list and adds an unknown name the server does not list', async () => {
    mockEnumSettings()
    renderPage()
    fireEvent.click(await screen.findByRole('tab', { name: 'Auth & Security' }))
    await waitForRow('allowed_users')

    openPicker('ALLOWED_USERS')
    const search = () => searchBox('ALLOWED_USERS')
    fireEvent.change(search(), { target: { value: 'ali' } })
    fireEvent.keyDown(search(), { key: 'Enter' })
    await waitFor(() =>
      expect(put).toHaveBeenLastCalledWith('/api/admin/settings', {
        settings: { allowed_users: 'alice' },
      }),
    )

    // An SSO user provisioned later is not in the table yet: free entry.
    fireEvent.change(search(), { target: { value: 'carol' } })
    const custom = screen.getByRole('option', { name: 'Add "carol"' })
    fireEvent.mouseDown(custom)
    await waitFor(() =>
      expect(put).toHaveBeenLastCalledWith('/api/admin/settings', {
        settings: { allowed_users: 'alice,carol' },
      }),
    )
    expect(screen.getByRole('button', { name: 'Remove carol' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Remove alice' })).toBeInTheDocument()
  })

  it('says the choices are loading while the list is still in flight', async () => {
    const settings = enumSettings()
    get.mockImplementation(async (url: unknown) => {
      if (String(url) === '/api/exporters') return new Promise(() => {}) as never
      return settings as never
    })
    renderPage()
    await waitForRow('auto_push_exporters')

    openPicker('AUTO_PUSH_EXPORTERS')
    expect(
      within(screen.getByRole('listbox')).getByText('Loading choices…'),
    ).toBeInTheDocument()
  })

  it('shows a stored value the list does not offer and lets the admin remove it', async () => {
    mockEnumSettings({
      log_level: { value: 'TRACE' },
      auto_push_exporters: { value: 'legacyplugin', source: 'db' },
    })
    renderPage()

    // Not offered by the server, but never silently dropped from the control.
    expect(await screen.findByRole('combobox', { name: 'Select LOG_LEVEL' })).toHaveTextContent(
      'TRACE',
    )
    const remove = await screen.findByRole('button', { name: 'Remove legacyplugin' })
    fireEvent.click(remove)
    await waitFor(() =>
      expect(put).toHaveBeenLastCalledWith('/api/admin/settings', {
        settings: { auto_push_exporters: '' },
      }),
    )
  })

  it('falls back to a plain input with a note when the list cannot be fetched, and still saves', async () => {
    mockEnumSettings({}, ['exporters'])
    renderPage()

    const input = await screen.findByRole('textbox', { name: 'Edit AUTO_PUSH_EXPORTERS' })
    expect(screen.getByText(/Could not load the choices from the server/)).toBeInTheDocument()
    expect(screen.queryByRole('combobox', { name: 'Add AUTO_PUSH_EXPORTERS' })).toBeNull()

    fireEvent.change(input, { target: { value: 'reportportal' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() =>
      expect(put).toHaveBeenLastCalledWith('/api/admin/settings', {
        settings: { auto_push_exporters: 'reportportal' },
      }),
    )
  })

  it('resolves dynamic choices under StrictMode double-mount (live-only regression)', async () => {
    // React StrictMode mounts, unmounts and remounts in development. The
    // options hook used to keep its "alive" flag false after that remount, so
    // every resolved fetch was discarded and the control stayed on
    // "Loading choices…" forever — a bug jsdom without StrictMode never saw.
    mockEnumSettings()
    render(
      <StrictMode>
        <MemoryRouter initialEntries={['/admin/settings']}>
          <ServerSettingsPage />
        </MemoryRouter>
      </StrictMode>,
    )

    await screen.findByRole('combobox', { name: 'Add AUTO_PUSH_EXPORTERS' })
    await waitFor(() =>
      expect(screen.getByRole('combobox', { name: 'Add AUTO_PUSH_EXPORTERS' })).not.toHaveTextContent(
        /Loading choices/i,
      ),
    )
  })
})

// ---------------------------------------------------------------------------
// E2E-F27 — rows are named for humans; E2E-F28..F30 — inline editors
// ---------------------------------------------------------------------------

/** The env var of a row, revealed by hovering its label (the only affordance). */
async function envVarTooltip(label: string): Promise<string> {
  await userEvent.setup().hover(screen.getByText(label))
  return (await screen.findByRole('tooltip')).textContent ?? ''
}

/** No row prints its env var: the line slot and its reserved height are gone. */
const expectNoEnvVarLine = () => {
  expect(document.querySelector('[data-setting-env-var-line]')).toBeNull()
  expect(document.querySelector('[data-setting-env-var]')).toBeNull()
}

/** The two structured editors live in the AI category. */
function structuredSettings(extra: Partial<Record<'additional_repos' | 'peer_ai_configs', string>> = {}) {
  return [
    mk('additional_repos', 'AI', extra.additional_repos ?? '', { label: 'Additional repositories' }),
    mk('peer_ai_configs', 'AI', extra.peer_ai_configs ?? '', { label: 'Peer AI configurations' }),
    mk('peer_analysis_max_rounds', 'AI', '3', { type: 'integer', label: 'Peer analysis max rounds' }),
  ]
}

async function openAiTab() {
  renderPage()
  fireEvent.click(await screen.findByRole('tab', { name: 'AI' }))
}

describe('ServerSettingsPage human labels (E2E-F27)', () => {
  it('titles a row with its label and hides the env var in the label tooltip', async () => {
    get.mockResolvedValue([
      mk('wait_for_completion', 'Server', 'true', { type: 'boolean', label: 'Wait for completion' }),
    ] as never)
    renderPage()

    const settingsRow = await waitFor(() => {
      const found = row('wait_for_completion')
      expect(found).not.toBeNull()
      return found as HTMLElement
    })
    // The title is the human name — the machine key is never the title.
    expect(settingsRow.querySelector('[data-setting-title]')?.textContent).toBe('Wait for completion')
    // No permanent second line: the env var is not body text on the row.
    expectNoEnvVarLine()
    expect(settingsRow.textContent).not.toContain('WAIT_FOR_COMPLETION')

    // Discoverable in the label's tooltip, monospaced and selectable (copyable).
    await userEvent.setup().hover(screen.getByText('Wait for completion'))
    const tooltip = await screen.findByRole('tooltip')
    const envVar = tooltip.querySelector('[data-setting-env-var]')
    expect(envVar?.textContent).toBe('WAIT_FOR_COMPLETION')
    expect(envVar?.className).toContain('font-mono')
    expect(envVar?.className).toContain('select-text')
  })

  it('renders no placeholder line for a row without an env var, so all rows stay one height', async () => {
    // A server that ships a setting without an env var (no deployment default).
    get.mockResolvedValue([
      mk('session_ttl_hours', 'Server', '24', { type: 'integer', env_var: '', label: 'Session lifetime' }),
      mk('wait_for_completion', 'Server', 'true', { type: 'boolean', label: 'Wait for completion' }),
    ] as never)
    renderPage()

    const without = await waitFor(() => {
      const found = row('session_ttl_hours')
      expect(found).not.toBeNull()
      return found as HTMLElement
    })
    // Neither row reserves a line — no fixed-height slot, no placeholder.
    expectNoEnvVarLine()
    expect(without.textContent).not.toContain('SESSION_TTL_HOURS')
  })

  it('gives an env-var-less row no tooltip at all', async () => {
    // jira_auth_mode ships without an env var: nothing to reveal, no dead hover.
    get.mockResolvedValue([
      mk('jira_auth_mode', 'Jira', 'pat', { env_var: '', label: 'Jira auth method' }),
      mk('jira_project_key', 'Jira', 'RC', { env_var: '', label: 'Jira project key' }),
    ] as never)
    renderPage()
    fireEvent.click(await screen.findByRole('tab', { name: 'Jira' }))

    await userEvent.setup().hover(await screen.findByText('Jira project key'))
    await new Promise((resolve) => setTimeout(resolve, 300))
    expect(screen.queryByRole('tooltip')).not.toBeInTheDocument()
  })

  it('gives jira_auth_mode no row of its own: label and env var live in the Jira card', async () => {
    get.mockResolvedValue([
      mk('jira_auth_mode', 'Jira', 'pat', { label: 'Jira auth method' }),
    ] as never)
    renderPage()
    fireEvent.click(await screen.findByRole('tab', { name: 'Jira' }))

    // The selector is the only editor, and the row list is empty beneath it.
    expect(await screen.findByRole('combobox', { name: 'Select Jira auth method' })).toBeInTheDocument()
    expect(row('jira_auth_mode')).toBeNull()
    // The card header reuses the same tooltip affordance, not a second copy.
    expect(await envVarTooltip('Jira auth method')).toBe('JIRA_AUTH_MODE')
  })

  it('finds a row by its env var as well as by its label', async () => {
    get.mockResolvedValue([
      mk('ai_call_timeout', 'AI', '600', { type: 'integer', label: 'AI call timeout' }),
      mk('jenkins_url', 'Jenkins', 'https://jenkins.example.com', { label: 'Jenkins URL' }),
    ] as never)
    renderPage()
    fireEvent.click(await screen.findByRole('tab', { name: 'AI' }))
    const search = await screen.findByLabelText('Search settings')

    // The env var is no longer visible, so search must find it (any case).
    fireEvent.change(search, { target: { value: 'ai_call_timeout' } })
    await waitFor(() => expect(shown('ai_call_timeout')).toBe(true))
    expect(shown('jenkins_url')).toBe(false)

    fireEvent.change(search, { target: { value: 'AI_CALL_TIMEOUT' } })
    await waitFor(() => expect(shown('ai_call_timeout')).toBe(true))

    // The label still matches.
    fireEvent.change(search, { target: { value: 'call timeout' } })
    await waitFor(() => expect(shown('ai_call_timeout')).toBe(true))
    fireEvent.change(search, { target: { value: 'jenkins' } })
    await waitFor(() => expect(shown('jenkins_url')).toBe(true))
    expect(shown('ai_call_timeout')).toBe(false)
  })
})

describe('ServerSettingsPage inline editors (E2E-F28..F30)', () => {
  it('shows the repositories editor inline, with nothing to save while it is empty', async () => {
    get.mockResolvedValue(structuredSettings() as never)
    await openAiTab()

    const repos = await waitFor(() => {
      const found = row('additional_repos')
      expect(found).not.toBeNull()
      return found as HTMLElement
    })
    // No link hides the editor any more.
    expect(within(repos).queryByRole('button', { name: /Configure repositories/i })).toBeNull()
    expect(within(repos).getByRole('button', { name: /Add Repository/i })).toBeInTheDocument()
    // Empty means there is nothing to save.
    expect(within(repos).queryByRole('button', { name: 'Save' })).toBeNull()
    expect(within(repos).queryByRole('button', { name: 'Cancel' })).toBeNull()
  })

  it('shows Save/Cancel after the first add and Cancel returns to the empty state', async () => {
    get.mockResolvedValue(structuredSettings() as never)
    await openAiTab()

    const repos = (await waitFor(() => row('additional_repos'))) as HTMLElement
    fireEvent.click(within(repos).getByRole('button', { name: /Add Repository/i }))

    expect(within(repos).getByPlaceholderText('Name')).toBeInTheDocument()
    expect(within(repos).getByRole('button', { name: 'Save' })).toBeInTheDocument()
    expect(within(repos).getByRole('button', { name: 'Cancel' })).toBeInTheDocument()

    fireEvent.click(within(repos).getByRole('button', { name: 'Cancel' }))
    expect(within(repos).queryByPlaceholderText('Name')).toBeNull()
    expect(within(repos).queryByRole('button', { name: 'Save' })).toBeNull()
    expect(within(repos).queryByRole('button', { name: 'Cancel' })).toBeNull()
    expect(within(repos).getByRole('button', { name: /Add Repository/i })).toBeInTheDocument()
  })

  it('posts the unchanged additional_repos payload and hides Save/Cancel again', async () => {
    // A stored repo with a ref: the parser/serialiser round trip must not
    // change a byte of what the server already holds.
    get.mockResolvedValue(
      structuredSettings({ additional_repos: 'grafana:https://github.com/acme/grafana:main' }) as never,
    )
    await openAiTab()

    const repos = (await waitFor(() => row('additional_repos'))) as HTMLElement
    // A stored value is a clean state too.
    expect(within(repos).queryByRole('button', { name: 'Save' })).toBeNull()

    fireEvent.click(within(repos).getByRole('button', { name: /Add Repository/i }))
    fireEvent.click(within(repos).getByRole('button', { name: 'Save' }))

    await waitFor(() =>
      expect(put).toHaveBeenLastCalledWith('/api/admin/settings', {
        settings: { additional_repos: 'grafana:https://github.com/acme/grafana:main' },
      }),
    )
    await waitFor(() => expect(within(repos).queryByRole('button', { name: 'Save' })).toBeNull())
    expect(within(repos).queryByRole('button', { name: 'Cancel' })).toBeNull()
  })

  it('shows the peer editor inline, with nothing to save while it is empty', async () => {
    get.mockResolvedValue(structuredSettings() as never)
    await openAiTab()

    const peers = await waitFor(() => {
      const found = row('peer_ai_configs')
      expect(found).not.toBeNull()
      return found as HTMLElement
    })
    expect(within(peers).queryByRole('button', { name: /Configure peers/i })).toBeNull()
    expect(within(peers).getByRole('button', { name: /Add peer configuration/i })).toBeInTheDocument()
    expect(within(peers).queryByRole('button', { name: 'Save' })).toBeNull()
    expect(within(peers).queryByRole('button', { name: 'Cancel' })).toBeNull()
  })

  it('shows Save/Cancel after the first peer add and Cancel restores the stored peers', async () => {
    get.mockResolvedValue(
      structuredSettings({ peer_ai_configs: 'gemini:gemini-2.5-pro,cursor:claude-4-sonnet' }) as never,
    )
    await openAiTab()

    const peers = (await waitFor(() => row('peer_ai_configs'))) as HTMLElement
    expect(within(peers).getByText('Peer 2')).toBeInTheDocument()

    fireEvent.click(within(peers).getByRole('button', { name: /Add peer configuration/i }))
    expect(within(peers).getByText('Peer 3')).toBeInTheDocument()
    expect(within(peers).getByRole('button', { name: 'Save' })).toBeInTheDocument()
    expect(within(peers).getByRole('button', { name: 'Cancel' })).toBeInTheDocument()

    fireEvent.click(within(peers).getByRole('button', { name: 'Cancel' }))
    expect(within(peers).queryByText('Peer 3')).toBeNull()
    expect(within(peers).getByText('Peer 2')).toBeInTheDocument()
    expect(within(peers).queryByRole('button', { name: 'Save' })).toBeNull()
    expect(within(peers).queryByRole('button', { name: 'Cancel' })).toBeNull()
  })

  it('posts the peer configs alone — rounds save through their own row (E2E-F33)', async () => {
    get.mockResolvedValue(
      structuredSettings({ peer_ai_configs: 'gemini:gemini-2.5-pro,cursor:claude-4-sonnet' }) as never,
    )
    await openAiTab()

    const peers = (await waitFor(() => row('peer_ai_configs'))) as HTMLElement
    fireEvent.click(within(peers).getByRole('button', { name: /Add peer configuration/i }))
    fireEvent.click(within(peers).getByRole('button', { name: 'Save' }))

    // Same comma-separated shape as before the change: the still-empty added
    // peer is dropped, and the card no longer carries the rounds setting.
    await waitFor(() =>
      expect(put).toHaveBeenLastCalledWith('/api/admin/settings', {
        settings: { peer_ai_configs: 'gemini:gemini-2.5-pro,cursor:claude-4-sonnet' },
      }),
    )
    await waitFor(() => expect(within(peers).queryByRole('button', { name: 'Save' })).toBeNull())
  })

  it('edits max rounds on its own row and no longer inside the peers card', async () => {
    get.mockResolvedValue(
      structuredSettings({ peer_ai_configs: 'gemini:gemini-2.5-pro' }) as never,
    )
    await openAiTab()

    const peers = (await waitFor(() => row('peer_ai_configs'))) as HTMLElement
    // The rounds input moved out of the card: one control per setting.
    expect(within(peers).queryByText('Max Rounds')).toBeNull()
    expect(screen.queryByText(/Managed via Peer AI Configs/)).toBeNull()

    // The row itself is the editor, like every other numeric setting.
    const rounds = (await waitFor(() => row('peer_analysis_max_rounds'))) as HTMLElement
    expect(within(rounds).getByRole('button', { name: 'Edit PEER_ANALYSIS_MAX_ROUNDS' })).toHaveTextContent(
      '3',
    )
    fireEvent.click(within(rounds).getByRole('button', { name: 'Edit PEER_ANALYSIS_MAX_ROUNDS' }))
    fireEvent.change(within(rounds).getByLabelText('Edit PEER_ANALYSIS_MAX_ROUNDS'), {
      target: { value: '6' },
    })
    fireEvent.click(within(rounds).getByRole('button', { name: 'Save' }))
    await waitFor(() =>
      expect(put).toHaveBeenLastCalledWith('/api/admin/settings', {
        settings: { peer_analysis_max_rounds: '6' },
      }),
    )
  })
})
