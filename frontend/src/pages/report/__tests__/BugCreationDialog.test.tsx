import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { ReportProvider } from '../ReportContext'
import { BugCreationDialog } from '../BugCreationDialog'
import { resetProviderCatalogCache } from '@/lib/useProviderOptions'
import { SERVER_ACCESS_HELP } from '@/components/shared/CredentialSourceLabel'

HTMLElement.prototype.hasPointerCapture = () => false
HTMLElement.prototype.setPointerCapture = () => {}
HTMLElement.prototype.releasePointerCapture = () => {}
HTMLElement.prototype.scrollIntoView = () => {}

const get = vi.fn()
const post = vi.fn()
let grant = true

vi.mock('@/lib/api', () => ({
  api: { get: (...args: unknown[]) => get(...args), post: (...args: unknown[]) => post(...args) },
  extractApiDetail: () => null,
}))
vi.mock('@/lib/cookies', () => ({
  getGithubToken: () => 'ghp_test_token',
  getJiraToken: () => '',
  getJiraEmail: () => '',
}))
vi.mock('@/lib/auth', () => ({
  useAuth: () => ({ role: 'operator', isOperator: true, isAdmin: false, username: 'alice', authenticated: true, canUseServerProviders: grant }),
}))

const catalog = {
  providers: {
    openai: [{ id: 'gpt-4o', name: 'GPT-4o', provider: 'openai', credential_sources: ['user'] }],
    claude: [{ id: 'sonnet', name: 'Sonnet', provider: 'claude', credential_sources: ['server'] }],
  },
  provider_status: {},
}

/** Waits for the issue-prompt fetch to land and the top-of-dialog picker to appear. */
async function openPicker(defaultProvider: string, defaultModel: string) {
  render(
    <MemoryRouter>
      <ReportProvider>
        <BugCreationDialog
          open
          onOpenChange={() => {}}
          jobId="job-1"
          testName="test-a"
          target="github"
          defaultAiProvider={defaultProvider}
          defaultAiModel={defaultModel}
        />
      </ReportProvider>
    </MemoryRouter>,
  )
  await waitFor(() => expect(screen.getByLabelText('Issue Prompt')).toBeInTheDocument())
  expect(screen.getByText('AI for issue generation')).toBeInTheDocument()
}

beforeEach(() => {
  vi.clearAllMocks()
  grant = true
  get.mockImplementation(async (path: string) => {
    if (path.startsWith('/api/ai-models')) return catalog
    if (path.endsWith('/issue-prompt')) return { issue_prompt: 'Describe the bug' }
    return {}
  })
  post.mockResolvedValue({ title: 'Issue title', body: 'Issue body', similar_issues: [] })
})

afterEach(() => {
  cleanup()
  resetProviderCatalogCache()
})

describe('BugCreationDialog AI picker', () => {
  it('seeds the analysis pair and labels every provider option with its credential source', async () => {
    const user = userEvent.setup()
    await openPicker('openai', 'gpt-4o')

    // Default intent: the pair the analysis used, now rendered with its source.
    expect(screen.getByRole('combobox', { name: 'AI Provider' })).toHaveTextContent(/openai.*User/i)
    expect(screen.getByRole('combobox', { name: 'AI Model' })).toHaveValue('gpt-4o')

    await user.click(screen.getByRole('combobox', { name: 'AI Provider' }))
    expect(await screen.findByRole('option', { name: /openai.*User/i })).toBeInTheDocument()
    expect(screen.getByRole('option', { name: /claude.*Server/i })).toBeInTheDocument()
  })

  it('gates a server-backed pair when the user has no server grant', async () => {
    grant = false
    const user = userEvent.setup()
    await openPicker('claude', 'sonnet')

    expect(screen.getAllByText(SERVER_ACCESS_HELP).length).toBeGreaterThan(0)
    await user.click(screen.getByRole('combobox', { name: 'AI Model' }))
    const serverModel = await screen.findByRole('option', { name: /sonnet/ })
    expect(serverModel).toHaveAttribute('aria-disabled', 'true')
    expect(screen.getByRole('button', { name: /continue/i })).toBeDisabled()
    expect(screen.getByRole('alert')).toHaveTextContent(/not available with your credentials/i)

    // A personal-key pair is usable, so the gate clears.
    await user.click(screen.getByRole('combobox', { name: 'AI Provider' }))
    await user.click(await screen.findByRole('option', { name: /openai.*User/i }))
    await user.click(screen.getByRole('combobox', { name: 'AI Model' }))
    await user.click(await screen.findByRole('option', { name: /gpt-4o/ }))
    expect(screen.getByRole('button', { name: /continue/i })).toBeEnabled()
  })

  it('sends the analysis pair with the preview request', async () => {
    const user = userEvent.setup()
    await openPicker('openai', 'gpt-4o')

    await user.click(screen.getByRole('button', { name: /continue/i }))
    await waitFor(() => expect(post).toHaveBeenCalled())
    expect(post).toHaveBeenCalledWith('/results/job-1/preview-github-issue',
      expect.objectContaining({ ai_provider: 'openai', ai_model: 'gpt-4o' }))
  })

  it('sends the pair chosen in the dialog', async () => {
    const user = userEvent.setup()
    await openPicker('openai', 'gpt-4o')

    await user.click(screen.getByRole('combobox', { name: 'AI Provider' }))
    await user.click(await screen.findByRole('option', { name: /claude.*Server/i }))
    expect(screen.getByRole('combobox', { name: 'AI Model' })).toHaveValue('')
    await user.click(screen.getByRole('combobox', { name: 'AI Model' }))
    await user.click(await screen.findByRole('option', { name: /sonnet/ }))

    await user.click(screen.getByRole('button', { name: /continue/i }))
    await waitFor(() => expect(post).toHaveBeenCalled())
    expect(post).toHaveBeenCalledWith('/results/job-1/preview-github-issue',
      expect.objectContaining({ ai_provider: 'claude', ai_model: 'sonnet' }))
  })

  it('leaves an empty selection to the server default', async () => {
    const user = userEvent.setup()
    await openPicker('', '')

    expect(screen.getByRole('combobox', { name: 'AI Provider' })).toHaveTextContent(/Select provider/i)
    expect(screen.getByRole('button', { name: /continue/i })).toBeEnabled()
    await user.click(screen.getByRole('button', { name: /continue/i }))
    await waitFor(() => expect(post).toHaveBeenCalled())
    expect(post).toHaveBeenCalledWith('/results/job-1/preview-github-issue',
      expect.objectContaining({ ai_provider: '', ai_model: '' }))
  })
})
