import { act, cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { ChatIssueDialog } from '@/components/shared/ChatIssueDialog'
import { ApiError } from '@/lib/api'

const get = vi.fn()
const post = vi.fn()
vi.mock('@/lib/api', async importOriginal => ({
  ...(await importOriginal<typeof import('@/lib/api')>()),
  api: { get: (...args: unknown[]) => get(...args), post: (...args: unknown[]) => post(...args) },
}))

const DRAFT = { title: 'Login test fails', body: 'The login test timed out resolving the auth service.' }
const CAPS = { github_issues_enabled: true, jira_issues_enabled: true }
const NO_TOKENS = { github_token: '', jira_token: '', jira_sa_token: '' }

function respond(caps: Record<string, boolean | string>, tokens: Record<string, string>) {
  get.mockImplementation(async (path: string) => {
    if (path === '/api/capabilities') return caps
    if (path === '/api/user/tokens') return tokens
    throw new Error(`unexpected GET ${path}`)
  })
  post.mockImplementation(async (path: string) =>
    path === '/api/chat-issue/prefill' ? DRAFT : { url: 'https://github.com/org/repo/issues/7', number: 7, key: '', title: DRAFT.title },
  )
}

function open() {
  return render(<ChatIssueDialog open onOpenChange={() => {}} message="raw chat answer" />)
}

afterEach(() => {
  cleanup()
  get.mockReset()
  post.mockReset()
  localStorage.clear()
})

describe('ChatIssueDialog', () => {
  it('prefills the scrubbed draft and files it with the account credential', async () => {
    // Browser storage is empty on purpose: availability follows the account row.
    respond(CAPS, { github_token: 'ghp_stored', jira_token: '', jira_sa_token: '' })
    open()
    const title = await screen.findByLabelText('Issue title')
    expect(title).toHaveValue(DRAFT.title)
    expect(screen.getByLabelText('Issue body')).toHaveValue(DRAFT.body)
    expect(post).toHaveBeenCalledWith('/api/chat-issue/prefill', { text: 'raw chat answer' })
    expect(localStorage.length).toBe(0)

    await userEvent.setup().click(screen.getByRole('button', { name: 'Create GitHub issue' }))
    expect(post).toHaveBeenCalledWith('/api/chat-issue', {
      target: 'github',
      title: DRAFT.title,
      body: DRAFT.body,
    })
    expect(await screen.findByText('https://github.com/org/repo/issues/7')).toBeInTheDocument()
  })

  it('offers no tracker when the account holds no credential', async () => {
    respond(CAPS, NO_TOKENS)
    open()
    expect(await screen.findByText(/Add a GitHub or Jira token in Profile Settings/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /^Create / })).not.toBeInTheDocument()
  })

  it('offers only a tracker the deployment enables and the account can use', async () => {
    respond({ github_issues_enabled: false, jira_issues_enabled: true, server_jira_project_key: 'MTV' }, {
      github_token: 'ghp_stored',
      jira_token: 'jira_stored',
      jira_sa_token: '',
    })
    open()
    await screen.findByLabelText('Issue title')
    expect(screen.queryByRole('button', { name: 'GitHub' })).not.toBeInTheDocument()
    await userEvent.setup().click(screen.getByRole('button', { name: 'Create Jira ticket' }))
    expect(post).toHaveBeenCalledWith('/api/chat-issue', {
      target: 'jira',
      title: DRAFT.title,
      body: DRAFT.body,
      jira_project_key: 'MTV',
    })
  })

  it('asks for a Jira project key when the server has no default and sends it', async () => {
    respond({ github_issues_enabled: false, jira_issues_enabled: true, server_jira_project_key: '' }, {
      github_token: '',
      jira_token: 'jira_stored',
      jira_sa_token: '',
    })
    open()
    const key = await screen.findByLabelText('Jira project key')
    expect(key).toHaveValue('')
    expect(screen.getByRole('button', { name: 'Create Jira ticket' })).toBeDisabled()
    await userEvent.setup().type(key, 'MTV')
    await userEvent.setup().click(screen.getByRole('button', { name: 'Create Jira ticket' }))
    expect(post).toHaveBeenCalledWith('/api/chat-issue', {
      target: 'jira',
      title: DRAFT.title,
      body: DRAFT.body,
      jira_project_key: 'MTV',
    })
  })

  it('shows nothing from the previous answer when a reopening prefill fails', async () => {
    respond(CAPS, { github_token: 'ghp_stored', jira_token: '', jira_sa_token: '' })
    const { rerender } = open()
    expect(await screen.findByLabelText('Issue title')).toHaveValue(DRAFT.title)
    rerender(<ChatIssueDialog open onOpenChange={() => {}} message="another answer" />)
    post.mockImplementation(async (path: string) => {
      if (path === '/api/chat-issue/prefill') {
        throw new ApiError(500, 'Internal Server Error', { detail: 'Failed to draft the issue' })
      }
      return { url: 'https://github.com/org/repo/issues/8' }
    })
    rerender(<ChatIssueDialog open onOpenChange={() => {}} message="third answer" />)
    expect(await screen.findByText('Failed to draft the issue')).toBeInTheDocument()
    expect(screen.queryByLabelText('Issue title')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /^Create / })).not.toBeInTheDocument()
  })

  it('shows the server refusal verbatim', async () => {
    respond(CAPS, { github_token: 'ghp_stored', jira_token: '', jira_sa_token: '' })
    post.mockImplementation(async (path: string) => {
      if (path === '/api/chat-issue/prefill') return DRAFT
      throw new ApiError(400, 'Bad Request', {
        detail: 'GitHub token is required. Set up your token in Profile Settings.',
      })
    })
    open()
    await screen.findByLabelText('Issue title')
    await userEvent.setup().click(screen.getByRole('button', { name: 'Create GitHub issue' }))
    expect(
      await screen.findByText('GitHub token is required. Set up your token in Profile Settings.'),
    ).toBeInTheDocument()
  })

  it('locks the tracker choice while a create is in flight', async () => {
    respond(CAPS, { github_token: 'ghp_stored', jira_token: 'jira_stored', jira_sa_token: '' })
    let finish: (value: unknown) => void = () => {}
    post.mockImplementation((path: string) =>
      path === '/api/chat-issue/prefill'
        ? Promise.resolve(DRAFT)
        : new Promise(resolve => {
            finish = resolve
          }),
    )
    open()
    await screen.findByLabelText('Issue title')
    await userEvent.setup().click(screen.getByRole('button', { name: 'Create GitHub issue' }))
    expect(screen.getByRole('button', { name: 'Jira' })).toBeDisabled()
    finish({ url: 'https://github.com/org/repo/issues/7' })
    await waitFor(() => expect(screen.getByRole('button', { name: 'Close' })).toBeInTheDocument())
  })

  it('ignores a create that settles after the dialog was reopened for another answer', async () => {
    const SECOND = { title: 'Auth timeout', body: 'The auth service timed out on the second answer.' }
    respond(CAPS, { github_token: 'ghp_stored', jira_token: '', jira_sa_token: '' })
    let finish: (value: unknown) => void = () => {}
    post.mockImplementation((path: string, body?: { text?: string }) => {
      if (path === '/api/chat-issue/prefill') {
        return Promise.resolve(body?.text === 'first answer' ? DRAFT : SECOND)
      }
      return new Promise(resolve => {
        finish = resolve
      })
    })
    const { rerender } = render(
      <ChatIssueDialog open onOpenChange={() => {}} message="first answer" />,
    )
    expect(await screen.findByLabelText('Issue title')).toHaveValue(DRAFT.title)
    await userEvent.setup().click(screen.getByRole('button', { name: 'Create GitHub issue' }))

    // Reopen for another answer while the first create is still in flight.
    rerender(<ChatIssueDialog open={false} onOpenChange={() => {}} message="first answer" />)
    rerender(<ChatIssueDialog open onOpenChange={() => {}} message="second answer" />)
    expect(await screen.findByLabelText('Issue title')).toHaveValue(SECOND.title)
    await act(async () => {
      finish({ url: 'https://github.com/org/repo/issues/7' })
    })

    expect(screen.getByLabelText('Issue title')).toHaveValue(SECOND.title)
    expect(screen.getByLabelText('Issue body')).toHaveValue(SECOND.body)
    expect(screen.queryByText('https://github.com/org/repo/issues/7')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Create GitHub issue' })).toBeEnabled()
  })

  it('does not submit twice', async () => {
    respond(CAPS, { github_token: 'ghp_stored', jira_token: '', jira_sa_token: '' })
    let finish: (value: unknown) => void = () => {}
    post.mockImplementation((path: string) =>
      path === '/api/chat-issue/prefill'
        ? Promise.resolve(DRAFT)
        : new Promise(resolve => {
            finish = resolve
          }),
    )
    open()
    await screen.findByLabelText('Issue title')
    const create = screen.getByRole('button', { name: 'Create GitHub issue' })
    const user = userEvent.setup()
    await user.click(create)
    await user.click(create)
    expect(post.mock.calls.filter(([path]) => path === '/api/chat-issue')).toHaveLength(1)
    finish({ url: 'https://github.com/org/repo/issues/7', number: 7, key: '', title: DRAFT.title })
    await waitFor(() => expect(screen.getByRole('button', { name: 'Close' })).toBeInTheDocument())
  })
})
