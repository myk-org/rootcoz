import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { ProfileForm } from '@/components/shared/ProfileForm'
import { setGithubToken } from '@/lib/cookies'

const apiMock = vi.hoisted(() => ({
  get: vi.fn(async (path: string) => (path === '/api/user/tokens' ? { github_token: '', jira_email: '', jira_token: '' } : {})),
  post: vi.fn(async (path: string) => (path === '/api/validate-token' ? { valid: true, username: 'jdoe', message: 'ok' } : {})),
  put: vi.fn(async () => ({})),
  delete: vi.fn(async () => ({})),
}))
vi.mock('@/lib/api', () => ({ api: apiMock, isExpectedTokenSyncError: () => true }))

const role = vi.hoisted(() => ({ value: 'viewer' }))
vi.mock('@/lib/auth', () => ({ useAuth: () => ({ username: 'jdoe', role: role.value, isAdmin: false }) }))

function renderForm() {
  const onSaved = vi.fn()
  render(<ProfileForm onSaved={onSaved} readOnlyUsername />)
  return onSaved
}

describe('ProfileForm tracker tokens', () => {
  beforeEach(() => {
    localStorage.clear()
    apiMock.get.mockClear()
    apiMock.post.mockClear()
    apiMock.put.mockClear()
    role.value = 'viewer'
  })

  it('hides tracker token fields from viewers', async () => {
    renderForm()
    await waitFor(() => expect(apiMock.get).toHaveBeenCalledWith('/api/user/tokens'))
    expect(screen.queryByText('Tracker Tokens')).not.toBeInTheDocument()
    expect(screen.queryByLabelText(/GitHub Token/i)).not.toBeInTheDocument()
  })

  it('shows tracker token fields to reviewers', () => {
    role.value = 'reviewer'
    renderForm()
    expect(screen.getByText('Tracker Tokens')).toBeInTheDocument()
    expect(screen.getByLabelText(/GitHub Token/i)).toBeInTheDocument()
  })

  it('never syncs tokens to the server for a viewer, even with a token in the cookie', async () => {
    setGithubToken('ghp_from_cookie')
    const onSaved = renderForm()
    await waitFor(() => expect(apiMock.get).toHaveBeenCalledWith('/api/user/tokens'))

    await userEvent.click(screen.getByRole('button', { name: /^save$/i }))

    await waitFor(() => expect(onSaved).toHaveBeenCalled())
    expect(apiMock.put).not.toHaveBeenCalledWith('/api/user/tokens', expect.anything())
    expect(apiMock.post).not.toHaveBeenCalled()
  })

  it('syncs tokens to the server for a reviewer', async () => {
    role.value = 'reviewer'
    const onSaved = renderForm()
    const github = await screen.findByLabelText(/GitHub Token/i)
    await userEvent.clear(github)
    await userEvent.type(github, 'ghp_valid')
    await userEvent.click(screen.getByRole('button', { name: /^save$/i }))
    await waitFor(() => expect(onSaved).toHaveBeenCalled())
    expect(apiMock.put).toHaveBeenCalledWith('/api/user/tokens', expect.objectContaining({ github_token: 'ghp_valid' }))
  })

  it('sends empty strings when a reviewer clears every token', async () => {
    role.value = 'reviewer'
    const onSaved = renderForm()
    await userEvent.click(screen.getByRole('button', { name: /^save$/i }))
    await waitFor(() => expect(onSaved).toHaveBeenCalled())
    expect(apiMock.put).toHaveBeenCalledWith('/api/user/tokens', {
      github_token: '',
      jira_email: '',
      jira_token: '',
    })
  })

  it('validates when only the Jira email changes (#294)', async () => {
    role.value = 'reviewer'
    apiMock.get.mockImplementation(async (path: string) =>
      path === '/api/user/tokens'
        ? { github_token: '', jira_email: 'old@test.com', jira_token: 'jira_tok' }
        : {},
    )

    const onSaved = renderForm()
    const email = await screen.findByLabelText(/jira email/i)
    await userEvent.clear(email)
    await userEvent.type(email, 'new@test.com')
    await userEvent.click(screen.getByRole('button', { name: /^save$/i }))

    // Jira authenticates with the email, so changing it alone must re-check.
    await waitFor(() =>
      expect(apiMock.post).toHaveBeenCalledWith(
        '/api/validate-token',
        expect.objectContaining({ token_type: 'jira', email: 'new@test.com' }),
      ),
    )
    await waitFor(() => expect(onSaved).toHaveBeenCalled())
  })

  it('does not re-validate a token that matches the server (#294)', async () => {
    role.value = 'reviewer'
    apiMock.get.mockImplementation(async (path: string) =>
      path === '/api/user/tokens'
        ? { github_token: 'ghp_server_current', jira_email: '', jira_token: '' }
        : {},
    )

    const onSaved = renderForm()
    await screen.findByLabelText(/GitHub Token/i) // hydrated from the server
    await userEvent.click(screen.getByRole('button', { name: /^save$/i }))

    await waitFor(() => expect(onSaved).toHaveBeenCalled())
    expect(apiMock.post).not.toHaveBeenCalledWith('/api/validate-token', expect.anything())
  })

  it('validates a browser-cached token that the server no longer has (#294)', async () => {
    role.value = 'reviewer'
    setGithubToken('ghp_stale_local')
    apiMock.get.mockImplementation(async (path: string) =>
      path === '/api/user/tokens'
        ? { github_token: 'ghp_server_current', jira_email: '', jira_token: '' }
        : {},
    )

    const onSaved = renderForm()
    await userEvent.click(screen.getByRole('button', { name: /^save$/i }))

    // The local cookie disagrees with the server, so it is a new value to check.
    await waitFor(() =>
      expect(apiMock.post).toHaveBeenCalledWith(
        '/api/validate-token',
        expect.objectContaining({ token_type: 'github', token: 'ghp_stale_local' }),
      ),
    )
    await waitFor(() => expect(onSaved).toHaveBeenCalled())
  })

  it('clears one token even when an unchanged stored token is invalid (#294)', async () => {
    role.value = 'reviewer'
    // Server hands back a stored GitHub token that no longer validates.
    apiMock.get.mockImplementation(async (path: string) =>
      path === '/api/user/tokens'
        ? { github_token: 'ghp_revoked', jira_email: '', jira_token: '' }
        : {},
    )
    apiMock.post.mockImplementation(async (path: string) =>
      path === '/api/validate-token'
        ? { valid: false, username: '', message: 'Bad credentials' }
        : {},
    )

    const onSaved = renderForm()
    const jira = await screen.findByLabelText(/Jira Token/i)
    await userEvent.type(jira, 'jira_tok')
    await userEvent.clear(jira)
    await userEvent.click(screen.getByRole('button', { name: /^save$/i }))

    await waitFor(() => expect(onSaved).toHaveBeenCalled())
    expect(apiMock.put).toHaveBeenCalledWith(
      '/api/user/tokens',
      expect.objectContaining({ github_token: 'ghp_revoked', jira_token: '' }),
    )
  })
})
