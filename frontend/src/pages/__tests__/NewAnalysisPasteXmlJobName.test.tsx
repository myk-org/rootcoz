import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { AuthProvider } from '@/lib/AuthProvider'
import { NewAnalysisPage } from '@/pages/NewAnalysisPage'
import { resetProviderCatalogCache } from '@/lib/providerCatalog'

const get = vi.fn()
const post = vi.fn()
vi.mock('@/lib/api', async (importActual) => ({
  ...await importActual<typeof import('@/lib/api')>(),
  api: { get: (...args: unknown[]) => get(...args), post: (...args: unknown[]) => post(...args) },
}))

const defaults = {
  ai_provider: 'openai',
  ai_model: 'gpt',
  ai_call_timeout: 10,
  tests_repo_url: '',
  additional_repos: [],
  peer_ai_configs: [],
  peer_analysis_max_rounds: 3,
  jira_enabled: false,
  jira_url: '',
  jira_project_key: '',
  get_job_artifacts: false,
  jenkins_artifacts_max_size_mb: 50,
  wait_for_completion: false,
  poll_interval_minutes: 2,
  max_wait_minutes: 0,
}

function setupApi() {
  get.mockImplementation(async (path: string) => {
    if (path === '/api/auth/me') {
      return {
        username: 'alice',
        role: 'operator',
        is_admin: false,
        can_view_reports: true,
        can_use_server_providers: false,
      }
    }
    if (path === '/api/user/tokens') {
      return { github_token: '', jira_email: '', jira_token: '' }
    }
    if (path.startsWith('/api/ai-models')) {
      return {
        providers: {
          openai: [{ id: 'gpt', name: 'GPT', provider: 'openai', credential_sources: ['user'] }],
        },
        provider_status: {},
      }
    }
    return defaults
  })
  post.mockResolvedValue({ job_id: 'job-123' })
}

HTMLElement.prototype.hasPointerCapture = () => false
HTMLElement.prototype.setPointerCapture = () => {}
HTMLElement.prototype.releasePointerCapture = () => {}
HTMLElement.prototype.scrollIntoView = () => {}

afterEach(() => {
  act(() => resetProviderCatalogCache())
  get.mockReset()
  post.mockReset()
})

async function openPasteXmlTab() {
  setupApi()
  const user = userEvent.setup()
  render(
    <AuthProvider>
      <MemoryRouter>
        <NewAnalysisPage />
      </MemoryRouter>
    </AuthProvider>,
  )
  await user.click(await screen.findByRole('button', { name: 'Paste XML' }))
  const jobNameInput = screen.getByLabelText('Job Name') as HTMLInputElement
  const xmlTextarea = screen.getByPlaceholderText('Paste JUnit XML content...')
  const submitButton = screen.getByRole('button', { name: 'Submit Analysis' })
  return { user, jobNameInput, xmlTextarea, submitButton }
}

describe('NewAnalysisPage Paste XML Job Name', () => {
  it('auto-prefills Job Name from root <testsuite name> and submits job_name', async () => {
    const { user, jobNameInput, xmlTextarea, submitButton } = await openPasteXmlTab()

    fireEvent.change(xmlTextarea, {
      target: { value: '<testsuite name="qe-ui-check-submit"><testcase name="t1"/></testsuite>' },
    })

    await waitFor(() => expect(jobNameInput.value).toBe('qe-ui-check-submit'))
    await waitFor(() => expect(submitButton).toBeEnabled())
    await user.click(submitButton)

    await waitFor(() => expect(post).toHaveBeenCalledTimes(1))
    expect(post).toHaveBeenCalledWith(
      '/analyze',
      expect.objectContaining({
        type: 'file',
        job_name: 'qe-ui-check-submit',
      }),
    )
  })

  it('auto-prefills Job Name from nested <testsuite name> inside <testsuites>', async () => {
    const { user, jobNameInput, xmlTextarea, submitButton } = await openPasteXmlTab()

    fireEvent.change(xmlTextarea, {
      target: { value: '<testsuites><testsuite name="nested-suite"/></testsuites>' },
    })

    await waitFor(() => expect(jobNameInput.value).toBe('nested-suite'))
    await waitFor(() => expect(submitButton).toBeEnabled())
    await user.click(submitButton)

    await waitFor(() => expect(post).toHaveBeenCalledTimes(1))
    expect(post).toHaveBeenCalledWith(
      '/analyze',
      expect.objectContaining({
        type: 'file',
        job_name: 'nested-suite',
      }),
    )
  })

  it('preserves user manual Job Name over subsequent XML changes and submits explicit name', async () => {
    const { user, jobNameInput, xmlTextarea, submitButton } = await openPasteXmlTab()

    fireEvent.change(xmlTextarea, {
      target: { value: '<testsuite name="initial-suite"/>' },
    })
    await waitFor(() => expect(jobNameInput.value).toBe('initial-suite'))

    await user.clear(jobNameInput)
    await user.type(jobNameInput, 'manual-override-name')

    fireEvent.change(xmlTextarea, {
      target: { value: '<testsuite name="later-suite"/>' },
    })
    await new Promise((resolve) => setTimeout(resolve, 200))
    expect(jobNameInput.value).toBe('manual-override-name')

    await waitFor(() => expect(submitButton).toBeEnabled())
    await user.click(submitButton)

    await waitFor(() => expect(post).toHaveBeenCalledTimes(1))
    expect(post).toHaveBeenCalledWith(
      '/analyze',
      expect.objectContaining({
        type: 'file',
        job_name: 'manual-override-name',
      }),
    )
  })

  it('re-enables auto-prefill after user clears their manual Job Name back to empty', async () => {
    const { user, jobNameInput, xmlTextarea } = await openPasteXmlTab()

    await user.type(jobNameInput, 'custom-first')
    fireEvent.change(xmlTextarea, { target: { value: '<testsuite name="suite-a"/>' } })
    await new Promise((resolve) => setTimeout(resolve, 200))
    expect(jobNameInput.value).toBe('custom-first')

    await user.clear(jobNameInput)
    fireEvent.change(xmlTextarea, { target: { value: '<testsuite name="suite-b"/>' } })
    await waitFor(() => expect(jobNameInput.value).toBe('suite-b'))
  })

  it('leaves Job Name empty and omits job_name from payload when XML has no suite name', async () => {
    const { user, jobNameInput, xmlTextarea, submitButton } = await openPasteXmlTab()

    fireEvent.change(xmlTextarea, {
      target: { value: '<testsuite><testcase name="t1"/></testsuite>' },
    })

    expect(jobNameInput.value).toBe('')
    await waitFor(() => expect(submitButton).toBeEnabled())
    await user.click(submitButton)

    await waitFor(() => expect(post).toHaveBeenCalledTimes(1))
    const [, body] = post.mock.calls[0] as [string, Record<string, unknown>]
    expect(body.type).toBe('file')
    expect('job_name' in body).toBe(false)
  })

  it('omits job_name when Job Name contains only whitespace', async () => {
    const { user, jobNameInput, xmlTextarea, submitButton } = await openPasteXmlTab()

    fireEvent.change(xmlTextarea, { target: { value: '<testsuite/>' } })
    await user.type(jobNameInput, '   ')

    await waitFor(() => expect(submitButton).toBeEnabled())
    await user.click(submitButton)

    await waitFor(() => expect(post).toHaveBeenCalledTimes(1))
    const [, body] = post.mock.calls[0] as [string, Record<string, unknown>]
    expect('job_name' in body).toBe(false)
  })

  it('does not prefill hostile suite names from XML and omits job_name on submit', async () => {
    const { user, jobNameInput, xmlTextarea, submitButton } = await openPasteXmlTab()

    fireEvent.change(xmlTextarea, {
      target: { value: '<testsuite name="../etc/passwd"/>' },
    })
    expect(jobNameInput.value).toBe('')

    fireEvent.change(xmlTextarea, {
      target: { value: '<testsuite name="a/b"/>' },
    })
    expect(jobNameInput.value).toBe('')

    await waitFor(() => expect(submitButton).toBeEnabled())
    await user.click(submitButton)

    await waitFor(() => expect(post).toHaveBeenCalledTimes(1))
    const [, body] = post.mock.calls[0] as [string, Record<string, unknown>]
    expect('job_name' in body).toBe(false)
  })

  it('shows inline validation error and blocks submit for hostile explicit Job Name input', async () => {
    const { user, jobNameInput, xmlTextarea, submitButton } = await openPasteXmlTab()

    fireEvent.change(xmlTextarea, { target: { value: '<testsuite/>' } })
    await waitFor(() => expect(submitButton).toBeEnabled())

    await user.type(jobNameInput, '../etc/passwd')
    expect(jobNameInput.value).toBe('../etc/passwd')
    expect(jobNameInput).toHaveAttribute('aria-invalid', 'true')
    expect(screen.getByRole('alert')).toHaveTextContent(/path separators/)
    expect(submitButton).toBeDisabled()

    await user.clear(jobNameInput)
    await user.type(jobNameInput, 'a/b')
    expect(jobNameInput.value).toBe('a/b')
    expect(screen.getByRole('alert')).toHaveTextContent(/path separators/)
    expect(submitButton).toBeDisabled()
    expect(post).not.toHaveBeenCalled()
  })
})
