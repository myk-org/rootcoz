import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from '@/lib/api'
import { ServerSettingsPage } from '../ServerSettingsPage'

vi.mock('@/lib/api', () => ({
  api: { get: vi.fn(), put: vi.fn(), delete: vi.fn(), testIntegration: vi.fn() },
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
vi.mock('@/components/shared/FreeModelsCard', () => ({ FreeModelsCard: () => null }))
vi.mock('@/components/shared/CursorAuthBanner', () => ({ CursorAuthBanner: () => null }))
vi.mock('@/components/shared/ProviderSelect', () => ({ ProviderSelect: () => null }))
vi.mock('@/components/shared/ModelCombobox', () => ({ ModelCombobox: () => null }))
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
  jira_auth_mode?: string
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
  mk('jira_url', 'Jira', 'https://jira.example.com'),
  mk('jira_project_key', 'Jira', 'RC'),
  mk('jira_email', 'Jira', 'admin@example.com'),
  mk('jira_api_token', 'Jira', 'tok', { sensitive: true }),
  mk('jira_pat', 'Jira', '', { sensitive: true }),
  mk('jira_oauth_access_token', 'Jira', '', { sensitive: true }),
  mk('atlassian_oauth_cloud_id', 'Jira', ''),
  mk('atlassian_oauth_client_id', 'Jira', ''),
  mk('enable_jira', 'Jira', 'false', { type: 'boolean' }),
  mk('github_token', 'GitHub', 'gh_tok', { sensitive: true }),
  mk('reportportal_url', 'Report Portal', 'https://rp.example.com'),
  mk('prow_url', 'Prow', 'https://prow.example.com'),
  mk('admin_key', 'Auth & Security', 'key', { sensitive: true }),
  mk('vapid_public_key', 'Web Push', 'vk'),
]

const get = vi.mocked(api.get)
const put = vi.mocked(api.put)
const testIntegration = vi.mocked(api.testIntegration)

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
})

describe('ServerSettingsPage tabs and search', () => {
  it('renders categories as tabs in the specified order and switches content', async () => {
    renderPage()
    await screen.findByText('PUBLIC_BASE_URL')

    const tabs = screen.getAllByRole('tab')
    expect(tabs.map((t) => t.textContent)).toEqual([
      'Server', 'AI', 'Jenkins', 'Jira', 'GitHub', 'Report Portal', 'Prow',
      'Auth & Security', 'Web Push',
    ])

    // Default (Server) tab shows only Server settings
    expect(screen.queryByText('JENKINS_URL')).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole('tab', { name: 'Jira' }))
    expect(await screen.findByText('JIRA_URL')).toBeInTheDocument()
    expect(screen.getByText('JIRA_PROJECT_KEY')).toBeInTheDocument()
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
    expect(screen.queryByText('JIRA_URL')).not.toBeInTheDocument()
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

describe('ServerSettingsPage Jira auth method toggle', () => {
  it('shows only the selected method fields and reflects the active method badge', async () => {
    renderPage()
    fireEvent.click(await screen.findByRole('tab', { name: 'Jira' }))
    await screen.findByText('JIRA_URL')

    // Stored email + token → active method is basic
    expect(screen.getByText('Active: Email + API token')).toBeInTheDocument()
    expect(screen.getByText('JIRA_EMAIL')).toBeInTheDocument()
    expect(screen.queryByText('JIRA_PAT')).not.toBeInTheDocument()

    // Common fields stay visible regardless of method
    expect(screen.getByText('JIRA_URL')).toBeInTheDocument()
    expect(screen.getByText('JIRA_PROJECT_KEY')).toBeInTheDocument()

    // Switch to PAT → only PAT fields shown
    fireEvent.click(screen.getByRole('button', { name: 'PAT (Bearer)' }))
    expect(screen.getByText('JIRA_PAT')).toBeInTheDocument()
    expect(screen.queryByText('JIRA_EMAIL')).not.toBeInTheDocument()
    expect(screen.queryByText('JIRA_OAUTH_ACCESS_TOKEN')).not.toBeInTheDocument()
    expect(screen.getByText('JIRA_URL')).toBeInTheDocument()

    // Switch to OAuth → oauth fields shown, basic hidden
    fireEvent.click(screen.getByRole('button', { name: 'Atlassian OAuth (token / cloud ID)' }))
    expect(screen.getByText('JIRA_OAUTH_ACCESS_TOKEN')).toBeInTheDocument()
    expect(screen.queryByText('JIRA_PAT')).not.toBeInTheDocument()
  })

  it('warns when editing a lower-precedence method while oauth is stored', async () => {
    const settings = baseSettings()
    settings.find((s) => s.key === 'jira_oauth_access_token')!.value = 'oa-token'
    get.mockResolvedValue(settings as never)

    renderPage()
    fireEvent.click(await screen.findByRole('tab', { name: 'Jira' }))
    await screen.findByText('JIRA_URL')

    // OAuth stored → active method is oauth
    expect(screen.getByText('Active: Atlassian OAuth (token / cloud ID)')).toBeInTheDocument()

    // Editing the lower-precedence basic method shows the precedence warning
    fireEvent.click(screen.getByRole('button', { name: 'Email + API token' }))
    expect(
      screen.getByText(
        'Atlassian OAuth (token / cloud ID) credentials are stored and take precedence — clear them to use this method.',
      ),
    ).toBeInTheDocument()
  })
})

describe('ServerSettingsPage test connection buttons', () => {
  it('renders test buttons only for the five integrable categories', async () => {
    renderPage()
    await screen.findByText('PUBLIC_BASE_URL')

    for (const tab of ['Jenkins', 'Jira', 'GitHub', 'Report Portal', 'Prow']) {
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

describe('ServerSettingsPage Jira method resolution', () => {
  async function openJiraTabWith(settings: TestSetting[]) {
    get.mockResolvedValue(settings as never)
    renderPage()
    fireEvent.click(await screen.findByRole('tab', { name: 'Jira' }))
    await screen.findByText('JIRA_URL')
  }

  it('treats a masked oauth token as the active oauth method', async () => {
    const settings = baseSettings()
    settings.find((s) => s.key === 'jira_oauth_access_token')!.value = '••••••••'
    await openJiraTabWith(settings)
    expect(screen.getByText('Active: Atlassian OAuth (token / cloud ID)')).toBeInTheDocument()
  })

  it('resolves email paired with only a PAT as pat (basic needs email AND api token)', async () => {
    const settings = baseSettings()
    settings.find((s) => s.key === 'jira_api_token')!.value = ''
    settings.find((s) => s.key === 'jira_pat')!.value = 'pat-without-prefix'
    await openJiraTabWith(settings)
    expect(screen.getByText('Active: PAT (Bearer)')).toBeInTheDocument()
  })

  it('shows no active method for an api token without email', async () => {
    const settings = baseSettings()
    settings.find((s) => s.key === 'jira_email')!.value = ''
    await openJiraTabWith(settings)
    expect(screen.queryByText(/^Active:/)).not.toBeInTheDocument()
  })

  it('prefers the backend-resolved jira_auth_mode over local inference', async () => {
    // Credentials alone would infer basic (email + api token), but the
    // backend's authoritative mode says pat — the badge must follow it.
    const settings = baseSettings()
    settings.find((s) => s.key === 'jira_url')!.jira_auth_mode = 'pat'
    await openJiraTabWith(settings)
    expect(screen.getByText('Active: PAT (Bearer)')).toBeInTheDocument()
  })
})

describe('ServerSettingsPage Jira field visibility', () => {
  it('finds auth-method-hidden Jira fields in search results', async () => {
    renderPage()
    await screen.findByText('PUBLIC_BASE_URL')

    // jira_pat is hidden while Email+token is the selected method
    fireEvent.click(screen.getByRole('tab', { name: 'Jira' }))
    expect(await screen.findByText('JIRA_URL')).toBeInTheDocument()
    expect(screen.queryByText('JIRA_PAT')).not.toBeInTheDocument()

    // Search shows every matching Jira field regardless of method hiding
    fireEvent.change(screen.getByLabelText('Search settings'), { target: { value: 'jira_pat' } })
    expect(await screen.findByText('JIRA_PAT')).toBeInTheDocument()
    // Header count matches the visible rows
    expect(screen.getByText('(1 setting)')).toBeInTheDocument()
  })

  it('cancels an open edit on a field hidden by the newly selected method', async () => {
    renderPage()
    fireEvent.click(await screen.findByRole('tab', { name: 'Jira' }))
    await screen.findByText('JIRA_URL')

    // Open an edit on a basic-method field
    fireEvent.click(screen.getByRole('button', { name: 'Edit JIRA_EMAIL' }))
    const testButton = screen.getByRole('button', { name: /test connection/i })
    expect(testButton).toBeDisabled()

    // Switching to PAT hides JIRA_EMAIL — the edit is canceled, not stranded
    fireEvent.click(screen.getByRole('button', { name: 'PAT (Bearer)' }))
    await waitFor(() => expect(testButton).toBeEnabled())
    expect(screen.queryByLabelText('Edit JIRA_EMAIL')).not.toBeInTheDocument()
    expect(screen.queryByText('JIRA_EMAIL')).not.toBeInTheDocument()
    expect(screen.getByText('JIRA_PAT')).toBeInTheDocument()
  })
})
