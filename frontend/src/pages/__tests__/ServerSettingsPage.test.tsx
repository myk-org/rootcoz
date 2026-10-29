import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { api, ApiError } from '@/lib/api'
import { useSSE } from '@/lib/useSSE'
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
vi.mock('@/lib/auth', () => ({ useAuth: () => ({ isAdmin: true, username: 'admin' }) }))
vi.mock('@/hooks/useProviderModels', () => ({
  useProviderModels: () => ({ models: [], refresh: vi.fn(), refreshing: false }),
}))
vi.mock('@/lib/useProviderOptions', () => ({ useCursorAuthStatus: () => null }))
vi.mock('@/components/shared/ServerProvidersCard', () => ({ ServerProvidersCard: () => null }))
vi.mock('@/components/shared/CursorAuthBanner', () => ({ CursorAuthBanner: () => null }))
vi.mock('@/components/shared/ProviderSelect', () => ({ ProviderSelect: () => null }))
vi.mock('@/components/shared/ModelCombobox', () => ({
  ModelCombobox: ({ className }: { className?: string }) => (
    <div data-testid="model-combobox" className={className} />
  ),
}))
vi.mock('@/components/shared/PeerConfigList', () => ({ PeerConfigList: () => null }))
vi.mock('@/components/shared/AdditionalReposList', () => ({ AdditionalReposList: () => null }))

interface TestSetting {
  key: string
  env_var: string
  value: string
  default: string
  description: string
  type: 'string' | 'boolean' | 'integer'
  category: string
  sensitive: boolean
  restart_required: boolean
  source: 'default' | 'env' | 'db'
  updated_by: string
  updated_at: string
  options?: string[]
  jira_verified_method?: string
  persistable?: boolean
  derived?: boolean
}

function mk(key: string, category: string, value = '', extra: Partial<TestSetting> = {}): TestSetting {
  return {
    key,
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

function renderPage() {
  return render(
    <MemoryRouter>
      <ServerSettingsPage />
    </MemoryRouter>,
  )
}

beforeEach(() => {
  vi.clearAllMocks()
  localStorage.clear()
  get.mockResolvedValue(baseSettings() as never)
  put.mockResolvedValue({} as never)
  testIntegration.mockResolvedValue({ ok: true, message: 'ok' } as never)
  persistSetting.mockResolvedValue({ key: '', source: 'db' } as never)
})

describe('ServerSettingsPage tabs and search', () => {
  it('renders categories as tabs in the specified order and switches content', async () => {
    renderPage()
    await screen.findByText('PUBLIC_BASE_URL')

    const tabs = screen.getAllByRole('tab')
    expect(tabs.map((t) => t.textContent)).toEqual([
      'Server', 'AI', 'Jenkins', 'Jira', 'GitHub', 'Report Portal', 'Datadog', 'Prow',
      'Auth & Security', 'Web Push',
    ])

    // Default (Server) tab shows only Server settings
    expect(screen.queryByText('JENKINS_URL')).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole('tab', { name: 'Jira' }))
    expect(await screen.findByText('JIRA_PROJECT_KEY')).toBeInTheDocument()
    expect(screen.queryByText('PUBLIC_BASE_URL')).not.toBeInTheDocument()

    // Active tab persists to localStorage
    expect(localStorage.getItem('rootcoz_settings_tab')).toBe('Jira')
  })

  it('switches to a flat search results view when searching', async () => {
    renderPage()
    await screen.findByText('PUBLIC_BASE_URL')

    fireEvent.change(screen.getByLabelText('Search settings'), { target: { value: 'jenkins_url' } })
    await waitFor(() => expect(screen.queryByRole('tab')).toBeNull())

    expect(screen.getByText('Search results')).toBeInTheDocument()
    expect(screen.getByText('JENKINS_URL')).toBeInTheDocument()
    expect(screen.queryByText('JIRA_PROJECT_KEY')).not.toBeInTheDocument()
  })
})

describe('ServerSettingsPage silent refetch on save', () => {
  it('keeps content mounted after a save (no loading skeleton re-appears)', async () => {
    renderPage()
    await screen.findByText('PUBLIC_BASE_URL')
    expect(get).toHaveBeenCalledTimes(1)

    // Toggle a boolean on the Server tab → immediate save + silent refetch
    fireEvent.click(screen.getByRole('switch', { name: 'Toggle WAIT_FOR_COMPLETION' }))
    await waitFor(() => expect(put).toHaveBeenCalledTimes(1))
    await waitFor(() => expect(get).toHaveBeenCalledTimes(2))

    // Content stayed mounted: rows still present, no skeleton
    expect(screen.getByText('PUBLIC_BASE_URL')).toBeInTheDocument()
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
    await screen.findByText('JIRA_PROJECT_KEY')
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

    expect(screen.getByRole('checkbox', { name: 'Email + API token' })).toBeChecked()
    expect(screen.getByRole('checkbox', { name: 'PAT (Bearer)' })).not.toBeChecked()
    expect(screen.getByRole('checkbox', { name: 'Atlassian OAuth (client secret / cloud ID)' })).not.toBeChecked()

    // Basic's four fields render (the SSL toggle renders as a switch)
    expect(screen.getByText('JIRA_EMAIL')).toBeInTheDocument()
    expect(screen.getByText('JIRA_API_TOKEN')).toBeInTheDocument()
    expect(screen.getByText('JIRA_CLOUD_URL')).toBeInTheDocument()
    expect(screen.getByRole('switch', { name: 'Toggle JIRA_CLOUD_SSL_VERIFY' })).toBeInTheDocument()

    // Other methods' fields are hidden
    expect(screen.queryByText('JIRA_PAT')).not.toBeInTheDocument()
    expect(screen.queryByText('JIRA_SERVER_URL')).not.toBeInTheDocument()
    expect(screen.queryByText('JIRA_SERVER_SSL_VERIFY')).not.toBeInTheDocument()
    expect(screen.queryByText('JIRA_OAUTH_CLIENT_SECRET')).not.toBeInTheDocument()
    expect(screen.queryByText('ATLASSIAN_OAUTH_CLOUD_ID')).not.toBeInTheDocument()
    expect(screen.queryByText('ATLASSIAN_OAUTH_CLIENT_ID')).not.toBeInTheDocument()
    expect(screen.queryByRole('switch', { name: 'Toggle JIRA_OAUTH_SSL_VERIFY' })).not.toBeInTheDocument()

    // The shared settings stay visible regardless of method
    expect(screen.getByText('JIRA_PROJECT_KEY')).toBeInTheDocument()
    expect(screen.getByText('JIRA_MAX_RESULTS')).toBeInTheDocument()
    expect(screen.getByText('JIRA_SITE_URL')).toBeInTheDocument()
    expect(screen.getByRole('switch', { name: 'Toggle ENABLE_JIRA' })).toBeInTheDocument()
    expect(screen.getByRole('switch', { name: 'Toggle ENABLE_JIRA_ISSUES' })).toBeInTheDocument()

    // The removed legacy settings no longer exist anywhere
    expect(screen.queryByText('JIRA_URL')).not.toBeInTheDocument()
    expect(screen.queryByText('JIRA_SSL_VERIFY')).not.toBeInTheDocument()
  })

  it('asks for confirmation when switching away from an active method; cancel keeps the old one', async () => {
    mockJiraModeSettings('basic')
    await openJiraTab()

    fireEvent.click(screen.getByRole('checkbox', { name: 'PAT (Bearer)' }))
    const dialog = await screen.findByRole('alertdialog')
    expect(dialog).toHaveTextContent('Switch from Email + API token to PAT (Bearer)?')
    expect(put).not.toHaveBeenCalled()

    fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(screen.queryByRole('alertdialog')).toBeNull())
    expect(put).not.toHaveBeenCalled()
    expect(screen.getByRole('checkbox', { name: 'Email + API token' })).toBeChecked()
    expect(screen.getByRole('checkbox', { name: 'PAT (Bearer)' })).not.toBeChecked()
  })

  it('confirm applies the switch: saves jira_auth_mode and unchecks the other method', async () => {
    mockJiraModeSettings('basic')
    await openJiraTab()

    fireEvent.click(screen.getByRole('checkbox', { name: 'PAT (Bearer)' }))
    fireEvent.click(within(await screen.findByRole('alertdialog')).getByRole('button', { name: 'Switch' }))

    await waitFor(() =>
      expect(put).toHaveBeenCalledWith('/api/admin/settings', { settings: { jira_auth_mode: 'pat' } }),
    )
    await waitFor(() => expect(screen.getByRole('checkbox', { name: 'PAT (Bearer)' })).toBeChecked())
    expect(screen.getByRole('checkbox', { name: 'Email + API token' })).not.toBeChecked()
    expect(screen.getByText('JIRA_PAT')).toBeInTheDocument()
    expect(screen.queryByText('JIRA_EMAIL')).not.toBeInTheDocument()
  })

  it('selecting a method when none is active applies without confirmation', async () => {
    mockJiraModeSettings('')
    await openJiraTab()
    expect(screen.queryByRole('alertdialog')).toBeNull()

    // No method selected: no method credential fields render, but
    // method-independent rows do
    expect(screen.queryByText('JIRA_PAT')).not.toBeInTheDocument()
    expect(screen.queryByText('JIRA_SERVER_URL')).not.toBeInTheDocument()
    expect(screen.queryByText('JIRA_EMAIL')).not.toBeInTheDocument()
    expect(screen.queryByText('JIRA_CLOUD_URL')).not.toBeInTheDocument()
    expect(screen.queryByText('JIRA_OAUTH_CLIENT_SECRET')).not.toBeInTheDocument()
    expect(screen.queryByText('ATLASSIAN_OAUTH_CLOUD_ID')).not.toBeInTheDocument()
    expect(screen.queryByText('ATLASSIAN_OAUTH_CLIENT_ID')).not.toBeInTheDocument()
    expect(screen.queryByRole('switch', { name: 'Toggle JIRA_OAUTH_SSL_VERIFY' })).not.toBeInTheDocument()
    expect(screen.getByText('JIRA_PROJECT_KEY')).toBeInTheDocument()
    expect(screen.getByText('JIRA_SITE_URL')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('checkbox', { name: 'Atlassian OAuth (client secret / cloud ID)' }))
    expect(screen.queryByRole('alertdialog')).toBeNull()

    await waitFor(() =>
      expect(put).toHaveBeenCalledWith('/api/admin/settings', { settings: { jira_auth_mode: 'oauth' } }),
    )
    await waitFor(() =>
      expect(screen.getByRole('checkbox', { name: 'Atlassian OAuth (client secret / cloud ID)' })).toBeChecked(),
    )
    expect(screen.getByRole('checkbox', { name: 'Email + API token' })).not.toBeChecked()
    expect(screen.getByText('JIRA_OAUTH_CLIENT_SECRET')).toBeInTheDocument()
    expect(screen.getByText('ATLASSIAN_OAUTH_CLOUD_ID')).toBeInTheDocument()
    expect(screen.getByText('ATLASSIAN_OAUTH_CLIENT_ID')).toBeInTheDocument()
    expect(screen.getByRole('switch', { name: 'Toggle JIRA_OAUTH_SSL_VERIFY' })).toBeInTheDocument()
    expect(screen.queryByText('JIRA_PAT')).not.toBeInTheDocument()
    expect(screen.queryByText('JIRA_EMAIL')).not.toBeInTheDocument()

    // Shared fields carry a hint that they are shared with the atst method
    expect(
      screen.getAllByText(
        'Shared with the Atlassian service account token (ATST, Bearer) method — both target the same Atlassian gateway tenant.',
      ),
    ).toHaveLength(2)
    // Non-shared rows (e.g. the oauth client secret) do not carry the hint
    expect(screen.getByText('JIRA_OAUTH_CLIENT_SECRET').closest('div.py-3')).not.toHaveTextContent(/Shared with the/)
  })

  it('selecting the atst method shows its fields and hides the other methods\' credentials', async () => {
    mockJiraModeSettings('')
    await openJiraTab()
    expect(screen.queryByRole('alertdialog')).toBeNull()

    fireEvent.click(screen.getByRole('checkbox', { name: 'Atlassian service account token (ATST, Bearer)' }))

    await waitFor(() =>
      expect(put).toHaveBeenCalledWith('/api/admin/settings', { settings: { jira_auth_mode: 'atst' } }),
    )
    await waitFor(() =>
      expect(screen.getByRole('checkbox', { name: 'Atlassian service account token (ATST, Bearer)' })).toBeChecked(),
    )
    expect(screen.getByText('JIRA_ATST_TOKEN')).toBeInTheDocument()
    // Shared locator/TLS fields stay visible for atst (same gateway as oauth)
    expect(screen.getByText('ATLASSIAN_OAUTH_CLOUD_ID')).toBeInTheDocument()
    expect(screen.getByRole('switch', { name: 'Toggle JIRA_OAUTH_SSL_VERIFY' })).toBeInTheDocument()
    // Shared fields carry a hint that they are shared with the oauth method
    expect(
      screen.getAllByText(
        'Shared with the Atlassian OAuth (client secret / cloud ID) method — both target the same Atlassian gateway tenant.',
      ),
    ).toHaveLength(2)
    // Non-shared rows (e.g. the ATST token) do not carry the hint
    expect(screen.getByText('JIRA_ATST_TOKEN').closest('div.py-3')).not.toHaveTextContent(/Shared with the/)
    // Other methods' credentials are hidden
    expect(screen.queryByText('JIRA_OAUTH_CLIENT_SECRET')).not.toBeInTheDocument()
    expect(screen.queryByText('ATLASSIAN_OAUTH_CLIENT_ID')).not.toBeInTheDocument()
    expect(screen.queryByText('JIRA_PAT')).not.toBeInTheDocument()
    expect(screen.queryByText('JIRA_EMAIL')).not.toBeInTheDocument()
  })

  it('switching away from a selected-but-unverified method applies immediately without confirmation', async () => {
    // basic is selected but its Test connection never passed (verified = '')
    mockJiraModeSettings('basic', '')
    await openJiraTab()

    fireEvent.click(screen.getByRole('checkbox', { name: 'PAT (Bearer)' }))
    expect(screen.queryByRole('alertdialog')).toBeNull()

    await waitFor(() =>
      expect(put).toHaveBeenCalledWith('/api/admin/settings', { settings: { jira_auth_mode: 'pat' } }),
    )
    await waitFor(() => expect(screen.getByRole('checkbox', { name: 'PAT (Bearer)' })).toBeChecked())
  })

  // ---- Refinement 6: the switch auto-runs the Jira test for the new method ----

  it('auto-runs the Jira test after switching to a configured method and shows the success state', async () => {
    mockJiraModeSettings('basic')
    testIntegration.mockResolvedValue({ ok: true, message: 'Jira connection verified' } as never)
    await openJiraTab()

    fireEvent.click(screen.getByRole('checkbox', { name: 'PAT (Bearer)' }))
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
    expect(screen.getByRole('checkbox', { name: 'PAT (Bearer)' })).toBeChecked()
  })

  it('auto-runs the Jira test after switching to an unconfigured method and surfaces the missing-fields error', async () => {
    mockJiraModeSettings('basic')
    testIntegration.mockResolvedValue({
      ok: false,
      message: "Jira auth method 'pat' requires jira_pat",
    } as never)
    await openJiraTab()

    fireEvent.click(screen.getByRole('checkbox', { name: 'PAT (Bearer)' }))
    fireEvent.click(within(await screen.findByRole('alertdialog')).getByRole('button', { name: 'Switch' }))

    await waitFor(() =>
      expect(put).toHaveBeenCalledWith('/api/admin/settings', { settings: { jira_auth_mode: 'pat' } }),
    )
    await waitFor(() => expect(testIntegration).toHaveBeenCalledWith('jira'))
    const banner = await screen.findByRole('status')
    expect(banner).toHaveTextContent('Connection failed')
    expect(banner).toHaveTextContent("requires jira_pat")
    // The failed auto-test surfaces the error but keeps the selection
    expect(screen.getByRole('checkbox', { name: 'PAT (Bearer)' })).toBeChecked()
  })

  it('does not run any test when switching to none (disabling Jira)', async () => {
    mockJiraModeSettings('basic')
    await openJiraTab()

    fireEvent.click(screen.getByRole('checkbox', { name: 'Email + API token' }))
    fireEvent.click(within(await screen.findByRole('alertdialog')).getByRole('button', { name: 'Disable' }))

    await waitFor(() =>
      expect(put).toHaveBeenCalledWith('/api/admin/settings', { settings: { jira_auth_mode: '' } }),
    )
    await waitFor(() =>
      expect(screen.getByRole('checkbox', { name: 'Email + API token' })).not.toBeChecked(),
    )
    expect(testIntegration).not.toHaveBeenCalled()
  })

  it('still auto-runs the test for the target method after confirming a switch away from a verified method', async () => {
    // basic is verified, so switching to pat goes through the confirmation dialog
    mockJiraModeSettings('basic')
    testIntegration.mockResolvedValue({ ok: true, message: 'ok' } as never)
    await openJiraTab()

    fireEvent.click(screen.getByRole('checkbox', { name: 'PAT (Bearer)' }))
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
    fireEvent.click(screen.getByRole('checkbox', { name: 'PAT (Bearer)' }))
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
    fireEvent.click(screen.getByRole('checkbox', { name: 'PAT (Bearer)' }))
    await waitFor(() =>
      expect(put).toHaveBeenCalledWith('/api/admin/settings', { settings: { jira_auth_mode: 'pat' } }),
    )
    await waitFor(() => expect(screen.getByRole('checkbox', { name: 'PAT (Bearer)' })).toBeChecked())
    expect(screen.getByText('JIRA_PAT')).toBeInTheDocument()
    expect(screen.queryByText('JIRA_EMAIL')).not.toBeInTheDocument()

    // The stale response (old method basic, now verified) arrives LAST —
    // it must be dropped: final state still reflects the pat switch.
    const stale = baseSettings()
    stale.find((s) => s.key === 'jira_auth_mode')!.value = 'basic'
    stale.find((s) => s.key === 'jira_auth_mode')!.jira_verified_method = 'basic'
    resolveStale(stale)
    await waitFor(() => expect(get).toHaveBeenCalledTimes(3))

    expect(screen.getByRole('checkbox', { name: 'PAT (Bearer)' })).toBeChecked()
    expect(screen.getByRole('checkbox', { name: 'Email + API token' })).not.toBeChecked()
    expect(screen.getByText('JIRA_PAT')).toBeInTheDocument()
    expect(screen.queryByText('JIRA_EMAIL')).not.toBeInTheDocument()
  })

  it('unchecking a selected-but-unverified method disables Jira immediately (no method fields render while unset)', async () => {
    mockJiraModeSettings('basic', '')
    await openJiraTab()

    fireEvent.click(screen.getByRole('checkbox', { name: 'Email + API token' }))
    expect(screen.queryByRole('alertdialog')).toBeNull()
    await waitFor(() =>
      expect(put).toHaveBeenCalledWith('/api/admin/settings', { settings: { jira_auth_mode: '' } }),
    )
    await waitFor(() =>
      expect(screen.getByRole('checkbox', { name: 'Email + API token' })).not.toBeChecked(),
    )
    expect(screen.getByText(/No method selected — Jira is disabled/)).toBeInTheDocument()
    expect(screen.queryByText('JIRA_PAT')).not.toBeInTheDocument()
    expect(screen.queryByText('JIRA_EMAIL')).not.toBeInTheDocument()
    expect(screen.queryByText('JIRA_OAUTH_CLIENT_SECRET')).not.toBeInTheDocument()
    // Method-independent rows stay visible
    expect(screen.getByText('JIRA_PROJECT_KEY')).toBeInTheDocument()
  })

  it('unchecking a verified method asks for confirmation and disables Jira on confirm', async () => {
    mockJiraModeSettings('basic') // basic selected and test-verified
    await openJiraTab()

    fireEvent.click(screen.getByRole('checkbox', { name: 'Email + API token' }))
    const dialog = await screen.findByRole('alertdialog')
    expect(dialog).toHaveTextContent(/disables Jira until another method is selected/)
    expect(put).not.toHaveBeenCalled()

    fireEvent.click(within(dialog).getByRole('button', { name: 'Disable' }))
    await waitFor(() =>
      expect(put).toHaveBeenCalledWith('/api/admin/settings', { settings: { jira_auth_mode: '' } }),
    )
    await waitFor(() =>
      expect(screen.getByRole('checkbox', { name: 'Email + API token' })).not.toBeChecked(),
    )
    expect(screen.getByText(/No method selected — Jira is disabled/)).toBeInTheDocument()
  })

  it('cancelling the disable confirmation keeps the method selected', async () => {
    mockJiraModeSettings('basic')
    await openJiraTab()

    fireEvent.click(screen.getByRole('checkbox', { name: 'Email + API token' }))
    fireEvent.click(within(await screen.findByRole('alertdialog')).getByRole('button', { name: 'Cancel' }))

    await waitFor(() => expect(screen.queryByRole('alertdialog')).toBeNull())
    expect(put).not.toHaveBeenCalled()
    expect(screen.getByRole('checkbox', { name: 'Email + API token' })).toBeChecked()
  })

  it('surfaces a backend save-validation error in the Jira section and keeps the selection', async () => {
    mockJiraModeSettings('basic')
    const err = new ApiError(400, 'Bad Request', null)
    err.status = 400
    err.body = { detail: "Jira auth method 'pat' requires jira_pat" }
    put.mockRejectedValueOnce(err as never)

    await openJiraTab()
    fireEvent.click(screen.getByRole('checkbox', { name: 'PAT (Bearer)' }))
    fireEvent.click(within(await screen.findByRole('alertdialog')).getByRole('button', { name: 'Switch' }))

    const alerts = await screen.findAllByRole('alert')
    expect(alerts.some((a) => a.textContent?.includes("requires jira_pat"))).toBe(true)
    // The failed switch is not applied
    expect(screen.getByRole('checkbox', { name: 'Email + API token' })).toBeChecked()
    expect(screen.getByRole('checkbox', { name: 'PAT (Bearer)' })).not.toBeChecked()
  })

  it('shows the disabled hint when no method is selected', async () => {
    mockJiraModeSettings('')
    await openJiraTab()
    expect(screen.getByText(/No method selected — Jira is disabled/)).toBeInTheDocument()
    // The hint never checks a box — only the persisted setting does
    expect(screen.getByRole('checkbox', { name: 'Email + API token' })).not.toBeChecked()
  })

  it('hides the disabled hint when a method is explicitly selected', async () => {
    mockJiraModeSettings('pat')
    await openJiraTab()
    expect(screen.queryByText(/No method selected/)).not.toBeInTheDocument()
    expect(screen.getByRole('checkbox', { name: 'PAT (Bearer)' })).toBeChecked()
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
    await screen.findByText('PUBLIC_BASE_URL')

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
    await screen.findByText('ACTIVE_SERVER_PROVIDERS')
  }

  it('shows the all-active default state with a checkbox list of options', async () => {
    await openAiTab()

    expect(screen.getByText('All server providers are active')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Configure providers' }))

    for (const provider of ['gemini', 'openai', 'vertex-claude']) {
      const box = screen.getByRole('checkbox', { name: provider })
      expect(box).toBeChecked()
    }
    expect(screen.getByText(/Unchecked providers are hidden from users/)).toBeInTheDocument()
  })

  it('saves the comma-joined list of checked providers when one is unchecked', async () => {
    await openAiTab()
    fireEvent.click(screen.getByRole('button', { name: 'Configure providers' }))

    fireEvent.click(screen.getByRole('checkbox', { name: 'openai' }))
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))

    await waitFor(() =>
      expect(put).toHaveBeenCalledWith('/api/admin/settings', {
        settings: { active_server_providers: 'gemini,vertex-claude' },
      }),
    )
  })

  it('saves an empty value (all active) when everything is checked', async () => {
    const settings = baseSettings()
    settings.find((s) => s.key === 'active_server_providers')!.value = 'gemini'
    get.mockResolvedValue(settings as never)

    await openAiTab()
    // Non-empty value renders chips: active solid, inactive struck through
    expect(screen.getByText('openai')).toHaveClass('line-through')

    fireEvent.click(screen.getByRole('button', { name: 'Configure providers' }))
    fireEvent.click(screen.getByRole('checkbox', { name: 'openai' }))
    fireEvent.click(screen.getByRole('checkbox', { name: 'vertex-claude' }))
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))

    await waitFor(() =>
      expect(put).toHaveBeenCalledWith('/api/admin/settings', {
        settings: { active_server_providers: '' },
      }),
    )
  })

  it('warns when zero providers are checked and saves empty (= all) on confirm', async () => {
    await openAiTab()
    fireEvent.click(screen.getByRole('button', { name: 'Configure providers' }))

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

  it('resets to all via the reset flow (restores the default empty value)', async () => {
    const settings = baseSettings()
    const providers = settings.find((s) => s.key === 'active_server_providers')!
    providers.value = 'gemini'
    providers.source = 'db'
    providers.updated_by = 'admin'
    get.mockResolvedValue(settings as never)

    await openAiTab()
    fireEvent.click(screen.getByRole('button', { name: 'Configure providers' }))

    // In-editor "Reset to all" re-checks everything
    fireEvent.click(screen.getByRole('checkbox', { name: 'gemini' }))
    fireEvent.click(screen.getByRole('button', { name: 'Reset to all' }))
    expect(screen.getByRole('checkbox', { name: 'gemini' })).toBeChecked()
    expect(screen.getByRole('checkbox', { name: 'openai' })).toBeChecked()

    // Setting reset (delete) restores the stored default
    const del = vi.mocked(api.delete)
    del.mockResolvedValue({} as never)
    fireEvent.click(screen.getByRole('button', { name: 'Reset' }))
    await waitFor(() =>
      expect(del).toHaveBeenCalledWith('/api/admin/settings/active_server_providers'),
    )
    // Silent refetch after reset
    await waitFor(() => expect(get).toHaveBeenCalledTimes(2))
  })

  it('closes the provider editor and resyncs selection after a setting reset, so Save cannot restore the old restriction', async () => {
    const settings = baseSettings()
    const providers = settings.find((s) => s.key === 'active_server_providers')!
    providers.value = 'gemini'
    providers.source = 'db'
    let reset = false
    get.mockImplementation(async () => (reset ? baseSettings() : settings))
    const del = vi.mocked(api.delete)
    del.mockImplementation(async () => {
      reset = true
      return {}
    })

    await openAiTab()
    fireEvent.click(screen.getByRole('button', { name: 'Configure providers' }))
    expect(screen.getByRole('checkbox', { name: 'openai' })).not.toBeChecked()

    fireEvent.click(screen.getByRole('button', { name: 'Reset' }))
    await waitFor(() => expect(get).toHaveBeenCalledTimes(2))

    // Editor closed after the reset: no Save button to click with stale state
    await waitFor(() =>
      expect(screen.queryByRole('button', { name: 'Save' })).not.toBeInTheDocument(),
    )
    expect(screen.getByRole('button', { name: 'Configure providers' })).toBeInTheDocument()

    // Re-opening starts from the refetched (reset) value, not the pre-reset selection
    fireEvent.click(screen.getByRole('button', { name: 'Configure providers' }))
    expect(screen.getByRole('checkbox', { name: 'openai' })).toBeChecked()
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() =>
      expect(put).toHaveBeenCalledWith('/api/admin/settings', {
        settings: { active_server_providers: '' },
      }),
    )
  })

  it('shows a hint when no server providers are available', async () => {
    const settings = baseSettings()
    settings.find((s) => s.key === 'active_server_providers')!.options = []
    get.mockResolvedValue(settings as never)

    await openAiTab()
    expect(screen.getByText('No server providers available')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Configure providers' })).not.toBeInTheDocument()
  })

  it('shows a reset affordance when a restriction is stored but discovery is unavailable', async () => {
    const settings = baseSettings()
    const providers = settings.find((s) => s.key === 'active_server_providers')!
    providers.options = []
    providers.value = 'gemini'
    providers.source = 'db'
    providers.updated_by = 'admin'
    get.mockResolvedValue(settings as never)

    await openAiTab()
    expect(screen.getByText('Restricted to:')).toBeInTheDocument()
    expect(
      screen.getByText(/Provider discovery is currently unavailable/),
    ).toBeInTheDocument()

    const del = vi.mocked(api.delete)
    del.mockResolvedValue({} as never)
    fireEvent.click(screen.getByRole('button', { name: 'Reset' }))
    await waitFor(() =>
      expect(del).toHaveBeenCalledWith('/api/admin/settings/active_server_providers'),
    )
    // Silent refetch after reset
    await waitFor(() => expect(get).toHaveBeenCalledTimes(2))
  })

  it('matches saved provider IDs case-insensitively and saves canonical spelling', async () => {
    const settings = baseSettings()
    settings.find((s) => s.key === 'active_server_providers')!.value = 'Gemini,OpenAI'
    get.mockResolvedValue(settings as never)

    await openAiTab()
    // Mixed-case saved IDs are active (no struck-through chip anywhere)
    for (const el of screen.getAllByText('gemini')) expect(el).not.toHaveClass('line-through')
    for (const el of screen.getAllByText('openai')) expect(el).not.toHaveClass('line-through')
    expect(screen.getByText('vertex-claude')).toHaveClass('line-through')

    fireEvent.click(screen.getByRole('button', { name: 'Configure providers' }))
    expect(screen.getByRole('checkbox', { name: 'gemini' })).toBeChecked()
    expect(screen.getByRole('checkbox', { name: 'openai' })).toBeChecked()
    expect(screen.getByRole('checkbox', { name: 'vertex-claude' })).not.toBeChecked()

    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() =>
      expect(put).toHaveBeenCalledWith('/api/admin/settings', {
        settings: { active_server_providers: 'gemini,openai' },
      }),
    )
  })

  it('does not repopulate provider checkboxes from a superseded reset refetch', async () => {
    const settings = baseSettings()
    const providers = settings.find((s) => s.key === 'active_server_providers')!
    providers.value = 'gemini'
    providers.source = 'db'
    get.mockResolvedValueOnce(settings as never) // initial load: restricted to gemini
    const del = vi.mocked(api.delete)
    del.mockResolvedValue({} as never)

    await openAiTab()
    fireEvent.click(screen.getByRole('button', { name: 'Configure providers' }))
    expect(screen.getByRole('checkbox', { name: 'openai' })).not.toBeChecked()

    // The reset's silent refetch stays pending; when it resolves it carries an
    // OLDER restriction than either the pre-reset or the post-reset state.
    let resolveStale!: (value: TestSetting[]) => void
    get.mockImplementationOnce(() => new Promise<TestSetting[]>((r) => { resolveStale = r }))
    fireEvent.click(screen.getByRole('button', { name: 'Reset' }))
    await waitFor(() => expect(get).toHaveBeenCalledTimes(2))

    // A newer fetch (settings-changed SSE) supersedes it and resolves with the
    // post-reset state: no restriction, all providers active.
    get.mockResolvedValueOnce(baseSettings() as never)
    const events = vi.mocked(useSSE).mock.calls.find(([stream]) => stream === 'settings')![1] as Record<string, () => void>
    events['settings-changed']()
    await waitFor(() => expect(get).toHaveBeenCalledTimes(3))

    // The stale response arrives LAST — handleReset must not repopulate the
    // provider checkboxes from it; the editor still closes (#346).
    const stale = baseSettings()
    stale.find((s) => s.key === 'active_server_providers')!.value = 'openai'
    resolveStale(stale)

    // Reset finished: editor closed, row shows the fresh (post-reset) state
    await waitFor(() =>
      expect(screen.queryByRole('button', { name: 'Save' })).not.toBeInTheDocument(),
    )
    expect(screen.getByText('All server providers are active')).toBeInTheDocument()

    // Reopening seeds from the rendered fresh value — never the stale
    // 'openai'-only restriction the superseded response carried.
    fireEvent.click(screen.getByRole('button', { name: 'Configure providers' }))
    for (const provider of ['gemini', 'openai', 'vertex-claude']) {
      expect(screen.getByRole('checkbox', { name: provider })).toBeChecked()
    }
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
    expect(screen.getByText('JENKINS_USER')).toBeInTheDocument()
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

  async function openTab(name: string, marker: string) {
    renderPage()
    fireEvent.click(await screen.findByRole('tab', { name }))
    await screen.findByText(marker)
  }

  it('clears the cached reveal on save — row shows the fresh value without a remount, and a new reveal shows the new value', async () => {
    const revealed = mockGet({ revealed: { jenkins_password: 'decrypted-old' } })
    await openTab('Jenkins', 'JENKINS_PASSWORD')

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
    await openTab('Jenkins', 'JENKINS_PASSWORD')

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
    await openTab('Jenkins', 'JENKINS_PASSWORD')

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
    await openTab('Jira', 'JIRA_API_TOKEN')

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

describe('ServerSettingsPage Jira field visibility', () => {
  it('finds auth-method-hidden Jira fields in search results', async () => {
    mockJiraModeSettings('basic')
    renderPage()
    await screen.findByText('PUBLIC_BASE_URL')

    // jira_pat is hidden while Email+token is the selected method
    fireEvent.click(screen.getByRole('tab', { name: 'Jira' }))
    expect(await screen.findByText('JIRA_PROJECT_KEY')).toBeInTheDocument()
    expect(screen.queryByText('JIRA_PAT')).not.toBeInTheDocument()

    // Search shows every matching Jira field regardless of method hiding
    fireEvent.change(screen.getByLabelText('Search settings'), { target: { value: 'jira_pat' } })
    expect(await screen.findByText('JIRA_PAT')).toBeInTheDocument()
    // Header count matches the visible rows
    expect(screen.getByText('(1 setting)')).toBeInTheDocument()
  })

  it('cancels an open edit on a field hidden by the newly selected method', async () => {
    mockJiraModeSettings('basic')
    renderPage()
    fireEvent.click(await screen.findByRole('tab', { name: 'Jira' }))
    await screen.findByText('JIRA_PROJECT_KEY')

    // Open an edit on a basic-method field
    fireEvent.click(screen.getByRole('button', { name: 'Edit JIRA_EMAIL' }))
    const testButton = screen.getByRole('button', { name: /test connection/i })
    expect(testButton).toBeDisabled()

    // Switching to PAT hides JIRA_EMAIL — the edit is canceled, not stranded
    fireEvent.click(screen.getByRole('checkbox', { name: 'PAT (Bearer)' }))
    fireEvent.click(within(await screen.findByRole('alertdialog')).getByRole('button', { name: 'Switch' }))
    await waitFor(() => expect(testButton).toBeEnabled())
    expect(screen.queryByLabelText('Edit JIRA_EMAIL')).not.toBeInTheDocument()
    expect(screen.queryByText('JIRA_EMAIL')).not.toBeInTheDocument()
    expect(screen.getByText('JIRA_PAT')).toBeInTheDocument()
  })

  it('keeps an open editor and its draft when switching to a method that still shows the field', async () => {
    mockJiraModeSettings('basic')
    renderPage()
    fireEvent.click(await screen.findByRole('tab', { name: 'Jira' }))
    await screen.findByText('JIRA_EMAIL')

    // Draft an edit on a method-independent field (visible under every method)
    fireEvent.click(screen.getByRole('button', { name: 'Edit JIRA_SITE_URL' }))
    fireEvent.change(screen.getByLabelText('Edit JIRA_SITE_URL'), {
      target: { value: 'https://draft.example.com' },
    })

    // Switch basic → PAT (verified method: confirmation dialog, then save)
    fireEvent.click(screen.getByRole('checkbox', { name: 'PAT (Bearer)' }))
    fireEvent.click(within(await screen.findByRole('alertdialog')).getByRole('button', { name: 'Switch' }))
    await waitFor(() =>
      expect(put).toHaveBeenCalledWith('/api/admin/settings', { settings: { jira_auth_mode: 'pat' } }),
    )
    await waitFor(() => expect(screen.getByRole('checkbox', { name: 'PAT (Bearer)' })).toBeChecked())

    // jira_site_url stays visible under PAT: the editor survives the switch with its draft
    expect(screen.getByLabelText('Edit JIRA_SITE_URL')).toHaveValue('https://draft.example.com')
    expect(screen.getByText('JIRA_PAT')).toBeInTheDocument()
    expect(screen.queryByText('JIRA_EMAIL')).not.toBeInTheDocument()
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
    await screen.findByText('PUBLIC_BASE_URL')

    const tabs = screen.getAllByRole('tab').map((t) => t.textContent)
    expect(tabs).toContain('Sidecar')
    expect(tabs).toContain('Notifications')

    fireEvent.click(screen.getByRole('tab', { name: 'Sidecar' }))
    expect(await screen.findByText('SIDECAR_HOST')).toBeInTheDocument()
    expect(screen.getByText('SIDECAR_PORT')).toBeInTheDocument()
    expect(screen.getByText('PI_SIDECAR_LOG_LEVEL')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('tab', { name: 'Notifications' }))
    expect(await screen.findByText('SMTP_HOST')).toBeInTheDocument()
    expect(screen.getByText('ALERT_EMAIL_TO')).toBeInTheDocument()
  })

  it('shows restart badges on every restart-tagged setting row', async () => {
    get.mockResolvedValue(paritySettings() as never)
    renderPage()
    await screen.findByText('PUBLIC_BASE_URL')

    // Sidecar tab: all 8 sidecar settings are restart-tagged
    fireEvent.click(screen.getByRole('tab', { name: 'Sidecar' }))
    await screen.findByText('SIDECAR_HOST')
    expect(screen.getAllByLabelText('Restart required')).toHaveLength(8)

    // Auth & Security: default_user_role carries the badge too
    fireEvent.click(screen.getByRole('tab', { name: 'Auth & Security' }))
    await screen.findByText('DEFAULT_USER_ROLE')
    expect(screen.getAllByLabelText('Restart required')).toHaveLength(1)
  })

  it('renders sensitive Notification rows masked with a reveal affordance', async () => {
    get.mockResolvedValue(paritySettings() as never)
    renderPage()
    fireEvent.click(await screen.findByRole('tab', { name: 'Notifications' }))
    await screen.findByText('SMTP_HOST')

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
    await screen.findByText('ACTIVE_SERVER_PROVIDERS')
    expect(screen.queryByText(/Free AI models/)).not.toBeInTheDocument()
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
    await screen.findByText('JENKINS_USER')
  }

  it('renders Save to DB only on env-sourced rows', async () => {
    get.mockResolvedValue(envSettings() as never)
    await renderJenkinsRows()

    expect(screen.getByText('JENKINS_URL')).toBeInTheDocument()
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
    await screen.findByText('DB_PATH')
    expect(screen.getByText('DEBUG')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Save to DB' })).not.toBeInTheDocument()

    // Auth & Security tab: admin_key likewise
    fireEvent.click(screen.getByRole('tab', { name: 'Auth & Security' }))
    await screen.findByText('ADMIN_KEY')
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
    await screen.findByText('SIDECAR_URL')
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
    await screen.findByText('PUBLIC_BASE_URL')
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
    await screen.findByText('PUBLIC_BASE_URL')
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
    await screen.findByText('PUBLIC_BASE_URL')

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
    await screen.findByText('PUBLIC_BASE_URL')

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
    await screen.findByText('PUBLIC_BASE_URL')

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
    await screen.findByText('PUBLIC_BASE_URL')

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
    await screen.findByText('JENKINS_PASSWORD')
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
    await screen.findByText('JENKINS_USER')
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
    await screen.findByText('PUBLIC_BASE_URL')

    const input = document.querySelector('input[type="file"]')!
    fireEvent.change(input, {
      target: { files: [new File(['not json'], 'settings.txt', { type: 'text/plain' })] },
    })
    expect(await screen.findByRole('alert')).toHaveTextContent('not a valid JSON file')
    expect(post).not.toHaveBeenCalled()
  })
})
