import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { ProfileForm } from '@/components/shared/ProfileForm'

const apiMock = vi.hoisted(() => ({
  get: vi.fn(async (path: string) => (path === '/api/user/tokens' ? { github_token: '', jira_email: '', jira_token: '', jira_sa_token: '' } : {})),
  post: vi.fn(async (path: string) => (path === '/api/validate-token' ? { valid: true, username: 'jdoe', message: 'ok' } : {})),
  put: vi.fn(async () => ({})),
  delete: vi.fn(async () => ({})),
  testUserTokens: vi.fn(async () => ({ ok: true, message: 'ok' })),
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
    // mockReset, not mockClear: these tests swap implementations per case and a
    // clear would let one test's response leak into the next.
    apiMock.get.mockReset()
    apiMock.get.mockImplementation(async (path: string) =>
      path === '/api/user/tokens' ? { github_token: '', jira_email: '', jira_token: '', jira_sa_token: '' } : {},
    )
    apiMock.post.mockReset()
    apiMock.post.mockImplementation(async (path: string) =>
      path === '/api/validate-token' ? { valid: true, username: 'jdoe', message: 'ok' } : {},
    )
    apiMock.put.mockClear()
    apiMock.testUserTokens.mockReset()
    apiMock.testUserTokens.mockImplementation(async () => ({ ok: true, message: 'ok' }))
    role.value = 'viewer'
  })

  it('hides tracker token fields from viewers', async () => {
    renderForm()
    await waitFor(() => expect(apiMock.get).toHaveBeenCalledWith('/api/user/tokens'))
    expect(screen.queryByText('Tracker Tokens')).not.toBeInTheDocument()
    expect(screen.queryByLabelText(/GitHub Token/i)).not.toBeInTheDocument()
  })

  it('opts tracker-token management fields into new-password autofill (Chrome does not insert saved logins)', () => {
    role.value = 'reviewer'
    renderForm()
    expect(screen.getByLabelText(/GitHub Token/i)).toHaveAttribute('autocomplete', 'new-password')
    expect(screen.getByLabelText(/Jira Token/i)).toHaveAttribute('autocomplete', 'new-password')
    expect(screen.getByLabelText(/Jira Service Account Token/i)).toHaveAttribute('autocomplete', 'new-password')
    expect(screen.getByLabelText(/jira email/i)).toHaveAttribute('autocomplete', 'email')
  })

  it('shows tracker token fields to reviewers', () => {
    role.value = 'reviewer'
    renderForm()
    expect(screen.getByText('Tracker Tokens')).toBeInTheDocument()
    expect(screen.getByLabelText(/GitHub Token/i)).toBeInTheDocument()
  })

  it('never syncs tokens to the server for a viewer', async () => {
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
      jira_sa_token: '',
    })
  })

  it('hydrates and persists the Jira service account token', async () => {
    role.value = 'reviewer'
    apiMock.get.mockImplementation(async (path: string) =>
      path === '/api/user/tokens'
        ? { github_token: '', jira_email: '', jira_token: '', jira_sa_token: 'ATST_stored' }
        : {},
    )
    const onSaved = renderForm()
    await waitFor(() => expect(apiMock.get).toHaveBeenCalledWith('/api/user/tokens'))

    await userEvent.click(screen.getByRole('button', { name: /^save$/i }))
    await waitFor(() => expect(onSaved).toHaveBeenCalled())
    expect(apiMock.put).toHaveBeenCalledWith('/api/user/tokens', expect.objectContaining({
      jira_sa_token: 'ATST_stored',
    }))
  })

  it('clears a stored Jira email without blocking on validation (#294)', async () => {
    role.value = 'reviewer'
    apiMock.get.mockImplementation(async (path: string) =>
      path === '/api/user/tokens'
        ? { github_token: '', jira_email: 'old@test.com', jira_token: 'jira_tok', jira_sa_token: '' }
        : {},
    )
    apiMock.post.mockImplementation(async (path: string) =>
      path === '/api/validate-token' ? { valid: false, username: '', message: 'Bad credentials' } : {},
    )

    const onSaved = renderForm()
    const email = await screen.findByLabelText(/jira email/i)
    await userEvent.clear(email)
    await userEvent.click(screen.getByRole('button', { name: /^save$/i }))

    // An email-only clear is not a credential change: validating with no email
    // switches the backend to bearer auth and would block the save.
    await waitFor(() => expect(onSaved).toHaveBeenCalled())
    expect(apiMock.post).not.toHaveBeenCalledWith('/api/validate-token', expect.anything())
    expect(apiMock.put).toHaveBeenCalledWith(
      '/api/user/tokens',
      expect.objectContaining({ jira_email: '', jira_token: 'jira_tok' }),
    )
  })

  it('validates when only the Jira email changes (#294)', async () => {
    role.value = 'reviewer'
    apiMock.get.mockImplementation(async (path: string) =>
      path === '/api/user/tokens'
        ? { github_token: '', jira_email: 'old@test.com', jira_token: 'jira_tok', jira_sa_token: '' }
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
        ? { github_token: 'ghp_server_current', jira_email: '', jira_token: '', jira_sa_token: '' }
        : {},
    )

    const onSaved = renderForm()
    await screen.findByLabelText(/GitHub Token/i) // hydrated from the server
    await userEvent.click(screen.getByRole('button', { name: /^save$/i }))

    await waitFor(() => expect(onSaved).toHaveBeenCalled())
    expect(apiMock.post).not.toHaveBeenCalledWith('/api/validate-token', expect.anything())
  })

  it('test_server_value_wins_over_a_stale_local_copy', async () => {
    role.value = 'reviewer'
    // A browser that upgraded still holds a copy — it must never win.
    localStorage.setItem('rootcoz_github_token', 'ghp_stale_local')
    apiMock.get.mockImplementation(async (path: string) =>
      path === '/api/user/tokens'
        ? { github_token: 'ghp_server', jira_email: '', jira_token: '', jira_sa_token: '' }
        : {},
    )

    const onSaved = renderForm()
    // The field shows what the server holds, not the stored copy.
    await waitFor(() =>
      expect(screen.getByLabelText(/GitHub Token/i)).toHaveValue('ghp_server'),
    )
    await userEvent.click(screen.getByRole('button', { name: /^save$/i }))

    await waitFor(() => expect(onSaved).toHaveBeenCalled())
    expect(apiMock.put).toHaveBeenCalledWith(
      '/api/user/tokens',
      expect.objectContaining({ github_token: 'ghp_server' }),
    )
    // The stale copy was never what the save sent.
    expect(apiMock.put).not.toHaveBeenCalledWith(
      '/api/user/tokens',
      expect.objectContaining({ github_token: 'ghp_stale_local' }),
    )
  })

  it('test_saving_leaves_no_token_in_storage', async () => {
    role.value = 'reviewer'
    const onSaved = renderForm()
    const github = await screen.findByLabelText(/GitHub Token/i)
    await userEvent.type(github, 'ghp_valid')
    await userEvent.click(screen.getByRole('button', { name: /^save$/i }))
    await waitFor(() => expect(onSaved).toHaveBeenCalled())

    const keys = Object.keys(localStorage).filter((k) => /token|email/.test(k))
    expect(keys).toEqual([])
  })

  it('never clears stored tokens when the account read fails', async () => {
    role.value = 'reviewer'
    apiMock.get.mockImplementation(async (path: string) => {
      if (path === '/api/user/tokens') throw new Error('server unavailable')
      return {}
    })

    const onSaved = renderForm()
    await waitFor(() => expect(apiMock.get).toHaveBeenCalledWith('/api/user/tokens'))
    await userEvent.click(screen.getByRole('button', { name: /^save$/i }))

    // The fields are empty because the read failed, not because the account is
    // empty: writing that back would erase the credentials the server holds,
    // and a save that writes nothing must not report success either.
    expect(
      await screen.findByText(/could not read your saved tracker credentials/i),
    ).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /^retry$/i })).toBeInTheDocument()
    expect(onSaved).not.toHaveBeenCalled()
    expect(apiMock.put).not.toHaveBeenCalled()
  })

  it('saves once the retried account read succeeds', async () => {
    role.value = 'reviewer'
    let failing = true
    apiMock.get.mockImplementation(async (path: string) => {
      if (path !== '/api/user/tokens') return {}
      if (failing) {
        failing = false
        throw new Error('server unavailable')
      }
      return { github_token: '', jira_email: '', jira_token: '', jira_sa_token: '' }
    })

    const onSaved = renderForm()
    const github = await screen.findByLabelText(/GitHub Token/i)
    await userEvent.type(github, 'ghp_after_retry')
    await userEvent.click(await screen.findByRole('button', { name: /^retry$/i }))
    await waitFor(() =>
      expect(
        screen.queryByText(/could not read your saved tracker credentials/i),
      ).not.toBeInTheDocument(),
    )

    await userEvent.click(screen.getByRole('button', { name: /^save$/i }))
    await waitFor(() => expect(onSaved).toHaveBeenCalled())
    expect(apiMock.put).toHaveBeenCalledWith(
      '/api/user/tokens',
      expect.objectContaining({ github_token: 'ghp_after_retry' }),
    )
  })

  it('keeps a credential typed before the account read lands', async () => {
    role.value = 'reviewer'
    let resolveTokens: (tokens: Record<string, string>) => void = () => {}
    apiMock.get.mockImplementation((path: string) =>
      path === '/api/user/tokens'
        ? new Promise<Record<string, string>>((resolve) => { resolveTokens = resolve })
        : Promise.resolve({}),
    )

    renderForm()
    const github = await screen.findByLabelText(/GitHub Token/i)
    await userEvent.type(github, 'ghp_typed')

    resolveTokens({ github_token: 'ghp_server', jira_email: '', jira_token: '', jira_sa_token: '' })
    // Save turns on only once the read has landed, so the field has taken it.
    const save = screen.getByRole('button', { name: /^save$/i })
    await waitFor(() => expect(save).toBeEnabled())
    expect(github).toHaveValue('ghp_typed')
  })

  it('clears one token even when an unchanged stored token is invalid (#294)', async () => {
    role.value = 'reviewer'
    // Server hands back a stored GitHub token that no longer validates.
    apiMock.get.mockImplementation(async (path: string) =>
      path === '/api/user/tokens'
        ? { github_token: 'ghp_revoked', jira_email: '', jira_token: '', jira_sa_token: '' }
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

describe('ProfileForm saved-token connection test (#537)', () => {
  beforeEach(() => {
    apiMock.testUserTokens.mockReset()
    apiMock.testUserTokens.mockImplementation(async () => ({ ok: true, message: 'ok' }))
    role.value = 'reviewer'
  })

  it('tests the saved GitHub token and shows a success banner', async () => {
    role.value = 'reviewer'
    apiMock.testUserTokens.mockResolvedValue({ ok: true, message: 'GitHub token is valid for jdoe' })
    renderForm()
    await waitFor(() => expect(apiMock.get).toHaveBeenCalledWith('/api/user/tokens'))

    await userEvent.click(screen.getByRole('button', { name: /test github connection/i }))

    await waitFor(() => expect(apiMock.testUserTokens).toHaveBeenCalledWith('github'))
    const banner = await screen.findByRole('status')
    expect(banner).toHaveTextContent('Connection successful')
    expect(banner).toHaveTextContent('GitHub token is valid for jdoe')
  })

  it('tests the saved Jira tokens once and shows the server message on failure', async () => {
    role.value = 'reviewer'
    apiMock.testUserTokens.mockResolvedValue({ ok: false, message: 'Jira rejected the API token' })
    renderForm()
    await waitFor(() => expect(apiMock.get).toHaveBeenCalledWith('/api/user/tokens'))

    await userEvent.click(screen.getByRole('button', { name: /test jira connection/i }))

    await waitFor(() => expect(apiMock.testUserTokens).toHaveBeenCalledWith('jira'))
    const banner = await screen.findByRole('status')
    expect(banner).toHaveTextContent('Connection failed')
    expect(banner).toHaveTextContent('Jira rejected the API token')
  })

  it('renders exactly one test button per connection (one GitHub, one Jira)', () => {
    renderForm()
    expect(screen.getAllByRole('button', { name: /connection/i })).toHaveLength(2)
    expect(screen.getAllByRole('button', { name: /test github connection/i })).toHaveLength(1)
    expect(screen.getAllByRole('button', { name: /test jira connection/i })).toHaveLength(1)
  })

  it('disables the button in flight and issues one request for two rapid clicks', async () => {
    role.value = 'reviewer'
    let resolveTest: (value: { ok: boolean; message: string }) => void = () => {}
    apiMock.testUserTokens.mockReturnValue(
      new Promise((resolve) => { resolveTest = resolve }),
    )
    renderForm()
    await waitFor(() => expect(apiMock.get).toHaveBeenCalledWith('/api/user/tokens'))

    const button = screen.getByRole('button', { name: /test github connection/i })
    fireEvent.click(button)
    fireEvent.click(button)

    await waitFor(() => expect(button).toBeDisabled())
    expect(apiMock.testUserTokens).toHaveBeenCalledTimes(1)

    resolveTest({ ok: true, message: 'ok' })
    await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('Connection successful'))
    expect(apiMock.testUserTokens).toHaveBeenCalledTimes(1)
  })

  it('refuses to test unsaved input: an edit disables the button until it is saved', async () => {
    role.value = 'reviewer'
    renderForm()
    await waitFor(() => expect(apiMock.get).toHaveBeenCalledWith('/api/user/tokens'))

    const github = screen.getByLabelText(/GitHub Token/i)
    await userEvent.type(github, 'ghp_unsaved')

    expect(screen.getByRole('button', { name: /test github connection/i })).toBeDisabled()
    // The Jira connection is untouched, so its test stays available.
    expect(screen.getByRole('button', { name: /test jira connection/i })).toBeEnabled()
  })

  it('drops a result that arrives after an edit invalidated its test', async () => {
    role.value = 'reviewer'
    let resolveTest: (value: { ok: boolean; message: string }) => void = () => {}
    apiMock.testUserTokens.mockReturnValue(
      new Promise((resolve) => { resolveTest = resolve }),
    )
    renderForm()
    await waitFor(() => expect(apiMock.get).toHaveBeenCalledWith('/api/user/tokens'))

    fireEvent.click(screen.getByRole('button', { name: /test github connection/i }))
    // An edit while the request is in flight: the answer describes the token
    // that was saved when it started, not what the fields now show.
    await userEvent.type(screen.getByLabelText(/GitHub Token/i), 'ghp_edited')

    resolveTest({ ok: true, message: 'GitHub token is valid for jdoe' })
    await waitFor(() => expect(apiMock.testUserTokens).toHaveBeenCalledTimes(1))
    await waitFor(() =>
      expect(screen.getByRole('button', { name: /test github connection/i })).toBeDisabled(),
    )
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
  })

  it('keeps both buttons disabled until the saved tokens have loaded', async () => {
    role.value = 'reviewer'
    let resolveTokens: (value: Record<string, string>) => void = () => {}
    apiMock.get.mockReturnValue(
      new Promise((resolve) => {
        resolveTokens = resolve as (value: Record<string, string>) => void
      }),
    )
    renderForm()

    const github = screen.getByRole('button', { name: /test github connection/i })
    const jira = screen.getByRole('button', { name: /test jira connection/i })
    // The fields are empty because nothing has been read yet, not because
    // nothing is saved — testing now would report a credential that exists.
    expect(github).toBeDisabled()
    expect(jira).toBeDisabled()

    resolveTokens({ github_token: 'ghp_saved', jira_email: '', jira_token: 'jira_saved', jira_sa_token: '' })
    await waitFor(() => expect(github).toBeEnabled())
    expect(jira).toBeEnabled()
  })

  it('never echoes a form token into the request or the banner', async () => {
    role.value = 'reviewer'
    apiMock.get.mockImplementation(async (path: string) =>
      path === '/api/user/tokens'
        ? { github_token: 'ghp_secret', jira_email: 'jdoe@test.com', jira_token: 'jira_secret', jira_sa_token: 'ATST_secret' }
        : {},
    )
    apiMock.testUserTokens.mockResolvedValue({ ok: true, message: 'Connected' })
    renderForm()
    await waitFor(() => expect(screen.getByLabelText(/GitHub Token/i)).toHaveValue('ghp_secret'))

    await userEvent.click(screen.getByRole('button', { name: /test github connection/i }))
    await waitFor(() => expect(apiMock.testUserTokens).toHaveBeenCalledWith('github'))
    await userEvent.click(screen.getByRole('button', { name: /test jira connection/i }))
    await waitFor(() => expect(apiMock.testUserTokens).toHaveBeenCalledWith('jira'))

    const banner = await screen.findByRole('status')
    for (const secret of ['ghp_secret', 'jira_secret', 'ATST_secret']) {
      expect(banner).not.toHaveTextContent(secret)
    }
    // The endpoint reads the caller's row: the body carries nothing at all.
    expect(apiMock.testUserTokens.mock.calls).toEqual([['github'], ['jira']])
  })
})
