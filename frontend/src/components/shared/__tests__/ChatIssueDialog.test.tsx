import { cleanup, render, screen, waitFor } from '@testing-library/react'
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

function respond(caps: Record<string, boolean>, tokens: Record<string, string>) {
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
    respond({ github_issues_enabled: false, jira_issues_enabled: true }, {
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
    })
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
