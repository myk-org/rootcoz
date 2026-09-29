import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, expect, it, vi } from 'vitest'
import { ChatUI } from '@/components/shared/ChatUI'
import { ApiError } from '@/lib/api'
import { resetProviderCatalogCache } from '@/lib/useProviderOptions'
import { useSSE } from '@/lib/SSEProvider'

const get = vi.fn()
const post = vi.fn()
const remove = vi.fn()
let grant = true
vi.mock('@/lib/api', async importOriginal => ({
  ...(await importOriginal<typeof import('@/lib/api')>()),
  api: { get: (...args: unknown[]) => get(...args), post: (...args: unknown[]) => post(...args), delete: (...args: unknown[]) => remove(...args) },
}))
vi.mock('@/lib/auth', () => ({ useAuth: () => ({ username: 'alice', isAdmin: false, authenticated: true, canUseServerProviders: grant }) }))
vi.mock('@/lib/SSEProvider', () => ({ useSSE: vi.fn() }))

HTMLElement.prototype.hasPointerCapture = () => false
HTMLElement.prototype.setPointerCapture = () => {}
HTMLElement.prototype.releasePointerCapture = () => {}
HTMLElement.prototype.scrollIntoView = () => {}

const history = { messages: [] as Array<{ id: number; job_id: string; role: string; content: string; username: string; ai_provider: string; ai_model: string; status: string; created_at: string }>, total: 0, active_session_version: 'no-session-version' }
const catalog = { providers: {
  openai: [{ id: 'gpt', name: 'GPT', provider: 'openai', credential_sources: ['user'] }],
  claude: [{ id: 'sonnet', name: 'Sonnet', provider: 'claude', credential_sources: ['server'] }],
  openrouter: [{ id: 'hint', name: 'Hint', provider: 'openrouter', credential_sources: ['user'], verified: false }],
}, provider_status: { openrouter: { has_api_key: true, modelListingSupported: false } } }
function setup(chat: Omit<typeof history, 'active_session_version'> & { active_session_version?: string; active_session?: { ai_provider: string; ai_model: string; credential_source: string } | null } = history) {
  get.mockImplementation(async (path: string) => path.startsWith('/api/ai-models') ? catalog : { active_session_version: chat.active_session ? 'session-version' : history.active_session_version, ...chat })
  post.mockResolvedValue({ ready: true, session_id: 'session', user_message: { id: 2, username: 'alice' }, assistant_message_id: 3 })
  remove.mockResolvedValue({})
}
function page(provider = 'openai', model = 'gpt') {
  return <ChatUI apiBasePath="/api/chat/job" sseTopic="chat:job" header={<h1>Job chat</h1>} defaultProvider={provider} defaultModel={model} />
}
afterEach(() => { cleanup(); grant = true; act(() => resetProviderCatalogCache()); get.mockReset(); post.mockReset(); remove.mockReset() })

it('loads history without POST init and starts only with a valid catalog pair', async () => {
  setup()
  const user = userEvent.setup()
  render(page('openai', 'missing'))
  expect(await screen.findByRole('button', { name: 'Start Chat' })).toBeDisabled()
  expect(get).toHaveBeenCalledWith('/api/chat/job')
  expect(post).not.toHaveBeenCalledWith('/api/chat/job/init', expect.anything())
  await user.click(screen.getByRole('combobox', { name: 'AI Model' }))
  await user.click(screen.getByRole('option', { name: /gpt.*User/i }))
  await user.click(screen.getByRole('button', { name: 'Start Chat' }))
  await waitFor(() => expect(post).toHaveBeenCalledWith('/api/chat/job/init', { ai_provider: 'openai', ai_model: 'gpt', force_server_credentials: false }))
  expect(screen.queryByRole('combobox', { name: 'AI Provider' })).not.toBeInTheDocument()
  expect(screen.getByText('openai / gpt · User')).toBeInTheDocument()
  await user.type(screen.getByPlaceholderText(/Ask a question/), 'hello')
  await user.click(screen.getByRole('button', { name: 'Send message' }))
  expect(post).toHaveBeenCalledWith('/api/chat/job', { message: 'hello', ai_provider: 'openai', ai_model: 'gpt', force_server_credentials: false })
})

it('requires grant for server selection, explains restriction, and pins server source in messages', async () => {
  setup()
  grant = false
  const user = userEvent.setup()
  const { unmount } = render(page('claude', 'sonnet'))
  expect(await screen.findByRole('button', { name: 'Start Chat' })).toBeDisabled()
  expect(screen.getByRole('switch', { name: 'Use server credentials' })).toBeDisabled()
  expect(screen.getByText(/Server credentials are restricted/)).toBeInTheDocument()
  expect(post).not.toHaveBeenCalledWith('/api/chat/job/init', expect.anything())
  unmount()
  grant = true
  render(page('claude', 'sonnet'))
  await user.click(screen.getByRole('switch', { name: 'Use server credentials' }))
  await waitFor(() => expect(screen.getByRole('button', { name: 'Start Chat' })).toBeEnabled())
  await user.click(screen.getByRole('button', { name: 'Start Chat' }))
  await waitFor(() => expect(post).toHaveBeenCalledWith('/api/chat/job/init', { ai_provider: 'claude', ai_model: 'sonnet', force_server_credentials: true }))
  await user.type(screen.getByPlaceholderText(/Ask a question/), 'hello')
  await user.click(screen.getByRole('button', { name: 'Send message' }))
  expect(post).toHaveBeenCalledWith('/api/chat/job', { message: 'hello', ai_provider: 'claude', ai_model: 'sonnet', force_server_credentials: true })
})

it('requires the server toggle for a granted server-only model even when the default catalog includes it', async () => {
  setup()
  const user = userEvent.setup()
  render(page('claude', 'sonnet'))
  await waitFor(() => expect(get).toHaveBeenCalledWith('/api/ai-models?force_server_credentials=false'))
  expect(screen.getByRole('button', { name: 'Start Chat' })).toBeDisabled()
  expect(screen.getByText(/Use server credentials to start with this model/)).toBeInTheDocument()
  await user.click(screen.getByRole('button', { name: 'Start Chat' }))
  expect(post).not.toHaveBeenCalledWith('/api/chat/job/init', expect.anything())
  await user.click(screen.getByRole('switch', { name: 'Use server credentials' }))
  await waitFor(() => expect(screen.getByRole('button', { name: 'Start Chat' })).toBeEnabled())
  await user.click(screen.getByRole('button', { name: 'Start Chat' }))
  expect(post).toHaveBeenCalledWith('/api/chat/job/init', { ai_provider: 'claude', ai_model: 'sonnet', force_server_credentials: true })
})

it('does not POST init after a history load failure', async () => {
  setup()
  get.mockImplementation(async (path: string) => {
    if (path.startsWith('/api/ai-models')) return catalog
    throw new Error('History unavailable')
  })
  render(page())
  expect(await screen.findByRole('alert')).toHaveTextContent('History unavailable')
  expect(screen.getByRole('button', { name: 'Start Chat' })).toBeDisabled()
  get.mockImplementation(async (path: string) => path.startsWith('/api/ai-models') ? catalog : history)
  await userEvent.setup().click(screen.getByRole('button', { name: 'Retry history' }))
  await waitFor(() => expect(screen.getByRole('button', { name: 'Start Chat' })).toBeEnabled())
  expect(post).not.toHaveBeenCalledWith('/api/chat/job/init', expect.anything())
})

it('retries history after confirmed init without reinitializing or deleting the session', async () => {
  const previous = { id: 1, job_id: 'job', role: 'user', content: 'previous question', username: 'alice', ai_provider: 'openai', ai_model: 'gpt', status: 'completed', created_at: '2026-01-01T00:00:00Z' }
  const existingHistory = { messages: [previous], total: 1 }
  setup(existingHistory)
  const user = userEvent.setup()
  let historyGets = 0
  get.mockImplementation(async (path: string) => {
    if (path.startsWith('/api/ai-models')) return catalog
    historyGets++
    if (historyGets === 2) throw new Error('History temporarily unavailable')
    return existingHistory
  })
  render(page())
  expect(await screen.findByText('previous question')).toBeInTheDocument()
  await waitFor(() => expect(screen.getByRole('button', { name: 'Start Chat' })).toBeEnabled())
  await user.click(screen.getByRole('button', { name: 'Start Chat' }))
  expect(await screen.findByRole('alert')).toHaveTextContent('History temporarily unavailable')
  expect(screen.getAllByText('previous question')).toHaveLength(1)
  expect(screen.queryByRole('button', { name: 'Start Chat' })).not.toBeInTheDocument()
  expect(screen.getByPlaceholderText(/Ask a question/)).toBeEnabled()
  expect(screen.getByRole('button', { name: 'Retry history' })).toBeEnabled()
  await user.click(screen.getByRole('button', { name: 'Retry history' }))
  await waitFor(() => expect(historyGets).toBe(3))
  expect(screen.getAllByText('previous question')).toHaveLength(1)
  expect(screen.queryByRole('button', { name: 'Start Chat' })).not.toBeInTheDocument()
  expect(screen.getByText('openai / gpt · User')).toBeInTheDocument()
  await user.type(screen.getByPlaceholderText(/Ask a question/), 'followup')
  await user.click(screen.getByRole('button', { name: 'Send message' }))
  expect(post).toHaveBeenCalledWith('/api/chat/job', { message: 'followup', ai_provider: 'openai', ai_model: 'gpt', force_server_credentials: false })
  expect(post.mock.calls.filter(([path]) => path === '/api/chat/job/init')).toHaveLength(1)
  expect(remove).not.toHaveBeenCalled()
})

it('does not duplicate a pending init or fetch history after unmount', async () => {
  setup()
  let finish!: (value: unknown) => void
  post.mockImplementation((path: string) => path.endsWith('/init')
    ? new Promise(resolve => { finish = resolve }) : Promise.resolve({}))
  const { unmount } = render(page())
  await waitFor(() => expect(screen.getByRole('button', { name: 'Start Chat' })).toBeEnabled())
  const startButton = screen.getByRole('button', { name: 'Start Chat' })
  expect(get.mock.calls.filter(([path]) => path === '/api/chat/job')).toHaveLength(1)
  fireEvent.click(startButton)
  fireEvent.click(startButton)
  expect(post.mock.calls.filter(([path]) => path === '/api/chat/job/init')).toHaveLength(1)
  expect(get.mock.calls.filter(([path]) => path === '/api/chat/job')).toHaveLength(1)
  unmount()
  await act(async () => { finish({ ready: true, session_id: 'session' }) })
  expect(get.mock.calls.filter(([path]) => path === '/api/chat/job')).toHaveLength(1)
})

it('does not POST init when no usable catalog pair exists', async () => {
  setup()
  const user = userEvent.setup()
  render(page('', ''))
  await screen.findByRole('button', { name: 'Start Chat' })
  await waitFor(() => expect(get).toHaveBeenCalledWith('/api/ai-models?force_server_credentials=false'))
  expect(screen.getByRole('button', { name: 'Start Chat' })).toBeDisabled()
  await user.click(screen.getByRole('combobox', { name: 'AI Provider' }))
  await user.click(screen.getByRole('option', { name: /Openai · User/i }))
  expect(screen.getByRole('button', { name: 'Start Chat' })).toBeDisabled()
  expect(post).not.toHaveBeenCalledWith('/api/chat/job/init', expect.anything())
})

it('shows existing history before starting and clears explicitly without reinitializing', async () => {
  setup({ total: 2, active_session: { ai_provider: 'openai', ai_model: 'gpt', credential_source: 'user' }, messages: [{ id: 1, job_id: 'job', role: 'user', content: 'previous question', username: 'alice', ai_provider: 'openai', ai_model: 'gpt', status: 'completed', created_at: '2026-01-01T00:00:00Z' }] })
  const user = userEvent.setup()
  render(page())
  expect(await screen.findByText('previous question')).toBeInTheDocument()
  expect(post).not.toHaveBeenCalledWith('/api/chat/job/init', expect.anything())
  expect(screen.getByPlaceholderText(/Ask a question/)).toBeEnabled()
  expect(screen.queryByRole('button', { name: 'Start Chat' })).not.toBeInTheDocument()
  expect(screen.getByText('openai / gpt · User')).toBeInTheDocument()
  await user.type(screen.getByPlaceholderText(/Ask a question/), 'followup')
  await user.click(screen.getByRole('button', { name: 'Send message' }))
  expect(post).toHaveBeenCalledWith('/api/chat/job', { message: 'followup', ai_provider: 'openai', ai_model: 'gpt', force_server_credentials: false })
  get.mockImplementation((path: string) => Promise.resolve(path.startsWith('/api/ai-models') ? catalog : { messages: [{ id: 3, job_id: 'job', role: 'assistant', content: 'done', username: '', ai_provider: 'openai', ai_model: 'gpt', status: 'completed', created_at: '2026-01-01T00:00:00Z' }], total: 1, active_session: { ai_provider: 'openai', ai_model: 'gpt', credential_source: 'user' }, active_session_version: 'current-version' }))
  act(() => { vi.mocked(useSSE).mock.lastCall![1]['chat-changed']('') })
  await screen.findByText('done')
  await user.click(screen.getByRole('button', { name: 'New Session' }))
  await waitFor(() => expect(remove).toHaveBeenCalledWith('/api/chat/job', undefined, { headers: { 'If-Match': 'current-version' } }))
  expect(post.mock.calls.filter(([path]) => path === '/api/chat/job/init')).toHaveLength(0)
  expect(screen.getByRole('combobox', { name: 'AI Provider' })).not.toBeDisabled()
  await waitFor(() => expect(screen.getByRole('button', { name: 'Start Chat' })).toBeEnabled())
})

it('restores a hidden init session with no visible messages', async () => {
  setup({ total: 1, active_session: { ai_provider: 'openai', ai_model: 'gpt', credential_source: 'user' }, messages: [] })
  render(page('', ''))
  await waitFor(() => expect(screen.getByPlaceholderText(/Ask a question/)).toBeEnabled())
  expect(screen.queryByRole('button', { name: 'Start Chat' })).not.toBeInTheDocument()
  expect(post).not.toHaveBeenCalledWith('/api/chat/job/init', expect.anything())
})

it('keeps orphaned history readable but requires Start before sending', async () => {
  setup({ total: 1, active_session: null, messages: [{ id: 1, job_id: 'job', role: 'user', content: 'old question', username: 'alice', ai_provider: 'openai', ai_model: 'gpt', status: 'completed', created_at: '2026-01-01T00:00:00Z' }] })
  const user = userEvent.setup()
  render(page())
  expect(await screen.findByText('old question')).toBeInTheDocument()
  await waitFor(() => expect(screen.getByRole('button', { name: 'Start Chat' })).toBeEnabled())
  expect(screen.getByPlaceholderText(/Ask a question/)).toBeDisabled()
  expect(remove).not.toHaveBeenCalled()
  await user.click(screen.getByRole('button', { name: 'Start Chat' }))
  expect(post).toHaveBeenCalledWith('/api/chat/job/init', { ai_provider: 'openai', ai_model: 'gpt', force_server_credentials: false })
  expect(remove).not.toHaveBeenCalled()
})

it('invalidates a stale selection when credential source changes', async () => {
  setup()
  const user = userEvent.setup()
  render(page('openai', 'gpt'))
  await waitFor(() => expect(screen.getByRole('button', { name: 'Start Chat' })).toBeEnabled())
  await user.click(screen.getByRole('switch', { name: 'Use server credentials' }))
  await waitFor(() => expect(screen.getByRole('button', { name: 'Start Chat' })).toBeDisabled())
  expect(screen.getByRole('combobox', { name: 'AI Model' })).toHaveValue('gpt (unavailable)')
  expect(post).not.toHaveBeenCalledWith('/api/chat/job/init', expect.anything())
})

it('blocks sends while clearing an existing session', async () => {
  setup({ total: 1, active_session: { ai_provider: 'openai', ai_model: 'gpt', credential_source: 'user' }, messages: [] })
  let finish!: (value: unknown) => void
  remove.mockImplementationOnce(() => new Promise(resolve => { finish = resolve }))
  const user = userEvent.setup()
  render(page())
  await waitFor(() => expect(screen.getByPlaceholderText(/Ask a question/)).toBeEnabled())
  await user.type(screen.getByPlaceholderText(/Ask a question/), 'hello')
  await user.click(screen.getByRole('button', { name: 'New Session' }))
  expect(screen.getByRole('button', { name: 'Clearing chat session' })).toBeDisabled()
  await act(async () => { finish({}); })
})

it('does not clear a session if delete fails', async () => {
  setup({ total: 1, active_session: { ai_provider: 'openai', ai_model: 'gpt', credential_source: 'user' }, messages: [] })
  remove.mockRejectedValueOnce(new Error('Could not clear'))
  const user = userEvent.setup()
  render(page())
  await waitFor(() => expect(screen.getByRole('button', { name: 'New Session' })).toBeEnabled())
  await user.click(screen.getByRole('button', { name: 'New Session' }))
  expect(await screen.findByRole('alert')).toHaveTextContent('Could not clear')
  expect(screen.queryByRole('button', { name: 'Start Chat' })).not.toBeInTheDocument()
  expect(screen.getByPlaceholderText(/Ask a question/)).toBeEnabled()
})

it('explains how to recover if init returns no session', async () => {
  setup()
  post.mockImplementation(async (path: string) => path.endsWith('/init') ? { ready: true, session_id: '' } : {})
  render(page())
  await waitFor(() => expect(screen.getByRole('button', { name: 'Start Chat' })).toBeEnabled())
  await userEvent.setup().click(screen.getByRole('button', { name: 'Start Chat' }))
  expect(await screen.findByRole('alert')).toHaveTextContent('Clear chat and Start a new session')
  expect(screen.getByPlaceholderText(/Ask a question/)).toBeDisabled()
})

it('keeps history visible and permits retry after Start fails', async () => {
  setup({ total: 0, messages: [] })
  post.mockImplementation((path: string) => path.endsWith('/init') ? Promise.reject(new Error('No usable credential')) : Promise.resolve({}))
  const user = userEvent.setup()
  render(page())
  await waitFor(() => expect(screen.getByRole('button', { name: 'Start Chat' })).toBeEnabled())
  await user.click(screen.getByRole('button', { name: 'Start Chat' }))
  expect(await screen.findByRole('alert')).toHaveTextContent('No usable credential')
  expect(screen.getByRole('button', { name: 'Start Chat' })).toBeEnabled()
  expect(screen.getByPlaceholderText(/Ask a question/)).toBeDisabled()
  expect(post.mock.calls.filter(([path]) => path === '/api/chat/job')).toHaveLength(0)
})

it.each(['event', 'reconnect'])('ignores a stale %s history GET after New Session succeeds', async trigger => {
  const old = { id: 7, job_id: 'job', role: 'user', content: 'deleted question', username: 'alice', ai_provider: 'openai', ai_model: 'gpt', status: 'completed', created_at: '2026-01-01T00:00:00Z' }
  setup({ messages: [old], total: 1, active_session: { ai_provider: 'openai', ai_model: 'gpt', credential_source: 'user' } })
  let finishGet!: (value: unknown) => void
  const stale = new Promise(resolve => { finishGet = resolve })
  const user = userEvent.setup()
  render(page())
  expect(await screen.findByText('deleted question')).toBeInTheDocument()
  await waitFor(() => expect(screen.getByRole('button', { name: 'New Session' })).toBeEnabled())
  let historyGets = 0
  get.mockImplementation((path: string) => path.startsWith('/api/ai-models') ? Promise.resolve(catalog) : ++historyGets === 1 ? stale : Promise.resolve({ messages: [old], total: 1, active_session: { ai_provider: 'openai', ai_model: 'gpt', credential_source: 'user' }, active_session_version: 'session-version' }))
  const sse = vi.mocked(useSSE).mock.lastCall!
  act(() => { if (trigger === 'event') sse[1]['chat-changed'](''); else sse[2]?.onReconnect?.() })
  await waitFor(() => expect(get.mock.calls.filter(([path]) => path === '/api/chat/job')).toHaveLength(2))
  await user.click(screen.getByRole('button', { name: 'New Session' }))
  await waitFor(() => expect(screen.queryByText('deleted question')).not.toBeInTheDocument())
  await act(async () => { finishGet({ messages: [old], total: 1 }) })
  expect(screen.queryByText('deleted question')).not.toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Start Chat' })).toBeEnabled()
})

it.each(['event', 'reconnect'])('syncs session metadata on %s after another tab clears, without reinitializing or hiding history', async trigger => {
  const old = { id: 7, job_id: 'job', role: 'user', content: 'old question', username: 'alice', ai_provider: 'openai', ai_model: 'gpt', status: 'completed', created_at: '2026-01-01T00:00:00Z' }
  setup({ messages: [old], total: 1, active_session: { ai_provider: 'openai', ai_model: 'gpt', credential_source: 'user' } })
  render(page())
  expect(await screen.findByText('old question')).toBeInTheDocument()
  await waitFor(() => expect(screen.getByPlaceholderText(/Ask a question/)).toBeEnabled())
  get.mockImplementation((path: string) => Promise.resolve(path.startsWith('/api/ai-models') ? catalog : { messages: [old], total: 1, active_session: null }))
  const sse = vi.mocked(useSSE).mock.lastCall!
  act(() => { if (trigger === 'event') sse[1]['chat-changed'](''); else sse[2]?.onReconnect?.() })
  await waitFor(() => expect(screen.getByRole('button', { name: 'Start Chat' })).toBeEnabled())
  expect(screen.getByText('old question')).toBeInTheDocument()
  expect(screen.getByPlaceholderText(/Ask a question/)).toBeDisabled()
  expect(post).not.toHaveBeenCalledWith('/api/chat/job/init', expect.anything())
  expect(remove).not.toHaveBeenCalled()
})

it('pins an externally started session from SSE history', async () => {
  setup()
  render(page())
  await waitFor(() => expect(screen.getByRole('button', { name: 'Start Chat' })).toBeEnabled())
  get.mockImplementation((path: string) => Promise.resolve(path.startsWith('/api/ai-models') ? catalog : { messages: [], total: 0, active_session: { ai_provider: 'claude', ai_model: 'sonnet', credential_source: 'server' } }))
  act(() => { vi.mocked(useSSE).mock.lastCall![1]['chat-changed']('') })
  await waitFor(() => expect(screen.getByText('claude / sonnet · Server')).toBeInTheDocument())
  await userEvent.setup().type(screen.getByPlaceholderText(/Ask a question/), 'hello{enter}')
  expect(post).toHaveBeenCalledWith('/api/chat/job', { message: 'hello', ai_provider: 'claude', ai_model: 'sonnet', force_server_credentials: true })
})

it('ignores an older SSE history response after a newer reconnect response', async () => {
  setup({ messages: [], total: 0, active_session: { ai_provider: 'openai', ai_model: 'gpt', credential_source: 'user' } })
  render(page())
  await waitFor(() => expect(screen.getByPlaceholderText(/Ask a question/)).toBeEnabled())
  let resolveOld!: (value: unknown) => void
  get.mockImplementationOnce(() => new Promise(resolve => { resolveOld = resolve }))
  const sse = vi.mocked(useSSE).mock.lastCall!
  act(() => { sse[1]['chat-changed']('') })
  get.mockImplementation((path: string) => Promise.resolve(path.startsWith('/api/ai-models') ? catalog : { messages: [], total: 0, active_session: null }))
  act(() => { sse[2]?.onReconnect?.() })
  await waitFor(() => expect(screen.getByRole('button', { name: 'Start Chat' })).toBeEnabled())
  await act(async () => { resolveOld({ messages: [], total: 0, active_session: { ai_provider: 'openai', ai_model: 'gpt', credential_source: 'user' } }) })
  expect(screen.getByRole('button', { name: 'Start Chat' })).toBeEnabled()
})

it('does not delete a replacement with different metadata when New Session sees stale history', async () => {
  setup({ messages: [], total: 0, active_session: { ai_provider: 'openai', ai_model: 'gpt', credential_source: 'user' } })
  render(page())
  await waitFor(() => expect(screen.getByPlaceholderText(/Ask a question/)).toBeEnabled())
  get.mockImplementation((path: string) => Promise.resolve(path.startsWith('/api/ai-models') ? catalog : { messages: [], total: 0, active_session: { ai_provider: 'claude', ai_model: 'sonnet', credential_source: 'server' } }))
  await userEvent.setup().click(screen.getByRole('button', { name: 'New Session' }))
  expect(remove).not.toHaveBeenCalled()
  expect(await screen.findByText('claude / sonnet · Server')).toBeInTheDocument()
})

it.each(['/api/chat/job', '/api/admin/chat'])('rejects a same-choice replacement between preflight and DELETE on %s and resyncs without clearing', async path => {
  const previous = { id: 7, job_id: 'job', role: 'user', content: 'kept question', username: 'alice', ai_provider: 'openai', ai_model: 'gpt', status: 'completed', created_at: '2026-01-01T00:00:00Z' }
  const replacement = { ...previous, id: 8, content: 'replacement question' }
  const active_session = { ai_provider: 'openai', ai_model: 'gpt', credential_source: 'user' }
  const oldHistory = { messages: [previous], total: 1, active_session, active_session_version: 'old-version' }
  const newHistory = { messages: [replacement], total: 1, active_session, active_session_version: 'new-version' }
  setup(oldHistory)
  const pageWithPath = <ChatUI apiBasePath={path} sseTopic="chat:job" header={<h1>Chat</h1>} />
  render(pageWithPath)
  expect(await screen.findByText('kept question')).toBeInTheDocument()
  let current = oldHistory
  get.mockImplementation(async (url: string) => url.startsWith('/api/ai-models') ? catalog : current)
  remove.mockImplementation(async (_url: string, _body: unknown, options: { headers: { 'If-Match': string } }) => {
    current = newHistory
    if (options.headers['If-Match'] !== current.active_session_version) throw new ApiError(409, 'Conflict', { detail: 'Chat session changed; refresh history before clearing' })
    return {}
  })
  await userEvent.setup().click(screen.getByRole('button', { name: 'New Session' }))
  await waitFor(() => expect(remove).toHaveBeenCalledWith(path, undefined, { headers: { 'If-Match': 'old-version' } }))
  expect(await screen.findByText('replacement question')).toBeInTheDocument()
  expect(screen.getByRole('alert')).toHaveTextContent('Chat session changed')
  expect(screen.getByText('openai / gpt · User')).toBeInTheDocument()
  expect(screen.getByPlaceholderText(/Ask a question/)).toBeEnabled()
  expect(screen.queryByRole('button', { name: 'Start Chat' })).not.toBeInTheDocument()
  expect(post).not.toHaveBeenCalledWith(`${path}/init`, expect.anything())
})

it('uses the no-session version when clearing orphaned history', async () => {
  const old = { id: 1, job_id: 'job', role: 'user', content: 'orphaned question', username: 'alice', ai_provider: 'openai', ai_model: 'gpt', status: 'completed', created_at: '2026-01-01T00:00:00Z' }
  setup({ messages: [old], total: 1, active_session: null, active_session_version: 'no-session-version' })
  render(page())
  expect(await screen.findByText('orphaned question')).toBeInTheDocument()
  await userEvent.setup().click(screen.getByRole('button', { name: 'New Session' }))
  await waitFor(() => expect(remove).toHaveBeenCalledWith('/api/chat/job', undefined, { headers: { 'If-Match': 'no-session-version' } }))
  expect(screen.queryByText('orphaned question')).not.toBeInTheDocument()
})

it('does not delete when an SSE update overtakes the New Session preflight', async () => {
  setup({ messages: [], total: 0, active_session: { ai_provider: 'openai', ai_model: 'gpt', credential_source: 'user' } })
  render(page())
  await waitFor(() => expect(screen.getByPlaceholderText(/Ask a question/)).toBeEnabled())
  let resolvePreflight!: (value: unknown) => void
  get.mockImplementationOnce(() => new Promise(resolve => { resolvePreflight = resolve }))
  fireEvent.click(screen.getByRole('button', { name: 'New Session' }))
  await waitFor(() => expect(resolvePreflight).toBeDefined())
  get.mockImplementation((path: string) => Promise.resolve(path.startsWith('/api/ai-models') ? catalog : { messages: [], total: 0, active_session: { ai_provider: 'claude', ai_model: 'sonnet', credential_source: 'server' } }))
  act(() => { vi.mocked(useSSE).mock.lastCall![1]['chat-changed']('') })
  await act(async () => { resolvePreflight({ messages: [], total: 0, active_session: { ai_provider: 'openai', ai_model: 'gpt', credential_source: 'user' } }) })
  expect(remove).not.toHaveBeenCalled()
  expect(await screen.findByText('claude / sonnet · Server')).toBeInTheDocument()
})

it('preserves history on failed DELETE despite a stale SSE GET', async () => {
  const old = { id: 7, job_id: 'job', role: 'user', content: 'kept question', username: 'alice', ai_provider: 'openai', ai_model: 'gpt', status: 'completed', created_at: '2026-01-01T00:00:00Z' }
  setup({ messages: [old], total: 1, active_session: { ai_provider: 'openai', ai_model: 'gpt', credential_source: 'user' } })
  const user = userEvent.setup()
  render(page())
  expect(await screen.findByText('kept question')).toBeInTheDocument()
  let finishGet!: (value: unknown) => void
  let historyGets = 0
  get.mockImplementation((path: string) => path.startsWith('/api/ai-models') ? Promise.resolve(catalog) : ++historyGets === 1 ? new Promise(resolve => { finishGet = resolve }) : Promise.resolve({ messages: [old], total: 1, active_session: { ai_provider: 'openai', ai_model: 'gpt', credential_source: 'user' }, active_session_version: 'session-version' }))
  act(() => { vi.mocked(useSSE).mock.lastCall![1]['chat-changed']('') })
  await waitFor(() => expect(finishGet).toBeDefined())
  remove.mockRejectedValueOnce(new Error('Could not clear'))
  await user.click(screen.getByRole('button', { name: 'New Session' }))
  expect(await screen.findByRole('alert')).toHaveTextContent('Could not clear')
  await act(async () => { finishGet({ messages: [], total: 0 }) })
  expect(screen.getByText('kept question')).toBeInTheDocument()
})

it('blocks rapid Enter and Send while POST is in flight and until a reply arrives', async () => {
  setup({ messages: [], total: 0, active_session: { ai_provider: 'openai', ai_model: 'gpt', credential_source: 'user' } })
  let finishPost!: (value: unknown) => void
  post.mockImplementation((path: string) => path === '/api/chat/job' ? new Promise(resolve => { finishPost = resolve }) : Promise.resolve({}))
  const user = userEvent.setup()
  render(page())
  const composer = await screen.findByPlaceholderText(/Ask a question/)
  await waitFor(() => expect(composer).toBeEnabled())
  await user.type(composer, 'hello')
  fireEvent.keyDown(composer, { key: 'Enter', code: 'Enter' })
  fireEvent.keyDown(composer, { key: 'Enter', code: 'Enter' })
  fireEvent.click(screen.getByRole('button', { name: /send message|wait for|type a message/i }))
  expect(post.mock.calls.filter(([path]) => path === '/api/chat/job')).toHaveLength(1)
  expect(composer).toBeDisabled()
  await act(async () => { finishPost({ user_message: { id: 2, username: 'alice' }, assistant_message_id: 3 }) })
  expect(composer).toBeDisabled()
  const reply = { id: 3, job_id: 'job', role: 'assistant', content: 'answer', username: '', ai_provider: 'openai', ai_model: 'gpt', status: 'completed', created_at: '2026-01-01T00:00:00Z' }
  get.mockImplementation((path: string) => Promise.resolve(path.startsWith('/api/ai-models') ? catalog : { messages: [reply], total: 1, active_session: { ai_provider: 'openai', ai_model: 'gpt', credential_source: 'user' } }))
  act(() => { vi.mocked(useSSE).mock.lastCall![1]['chat-changed']('') })
  expect(await screen.findByText('answer')).toBeInTheDocument()
  expect(composer).toBeEnabled()
})

it('does not queue a second POST on a double click', async () => {
  setup({ messages: [], total: 0, active_session: { ai_provider: 'openai', ai_model: 'gpt', credential_source: 'user' } })
  let finishPost!: (value: unknown) => void
  post.mockImplementation((path: string) => path === '/api/chat/job' ? new Promise(resolve => { finishPost = resolve }) : Promise.resolve({}))
  render(page())
  const composer = await screen.findByPlaceholderText(/Ask a question/)
  await waitFor(() => expect(composer).toBeEnabled())
  await userEvent.setup().type(composer, 'hello')
  const send = screen.getByRole('button', { name: 'Send message' })
  fireEvent.click(send)
  fireEvent.click(send)
  expect(post.mock.calls.filter(([path]) => path === '/api/chat/job')).toHaveLength(1)
  expect(send).toBeDisabled()
  await act(async () => { finishPost({ user_message: { id: 2, username: 'alice' }, assistant_message_id: 3 }) })
})

it('restores the composer when send POST fails', async () => {
  setup({ messages: [], total: 0, active_session: { ai_provider: 'openai', ai_model: 'gpt', credential_source: 'user' } })
  post.mockRejectedValueOnce(new Error('Send failed'))
  render(page())
  const composer = await screen.findByPlaceholderText(/Ask a question/)
  await waitFor(() => expect(composer).toBeEnabled())
  await userEvent.setup().type(composer, 'retry me{enter}')
  expect(await screen.findByRole('alert')).toHaveTextContent('Send failed')
  expect(composer).toBeEnabled()
  expect(composer).toHaveValue('retry me')
})

it('accepts manual model IDs only when a user key cannot list models', async () => {
  setup()
  const user = userEvent.setup()
  render(page('openrouter', ''))
  await screen.findByRole('button', { name: 'Start Chat' })
  await user.type(screen.getByRole('combobox', { name: 'AI Model' }), 'manual-id')
  expect(screen.getByRole('button', { name: 'Start Chat' })).toBeEnabled()
  await user.click(screen.getByRole('button', { name: 'Start Chat' }))
  expect(post).toHaveBeenCalledWith('/api/chat/job/init', { ai_provider: 'openrouter', ai_model: 'manual-id', force_server_credentials: false })
})

it('does not delete a same-metadata replacement started in another tab before preflight', async () => {
  const previous = { id: 7, job_id: 'job', role: 'user', content: 'kept question', username: 'alice', ai_provider: 'openai', ai_model: 'gpt', status: 'completed', created_at: '2026-01-01T00:00:00Z' }
  const replacement = { ...previous, id: 8, content: 'replacement question' }
  const active_session = { ai_provider: 'openai', ai_model: 'gpt', credential_source: 'user' }
  setup({ messages: [previous], total: 1, active_session, active_session_version: 'old-version' })
  render(page())
  expect(await screen.findByText('kept question')).toBeInTheDocument()
  get.mockImplementation((path: string) => Promise.resolve(path.startsWith('/api/ai-models')
    ? catalog
    : { messages: [replacement], total: 1, active_session, active_session_version: 'replacement-version' }))
  await userEvent.setup().click(screen.getByRole('button', { name: 'New Session' }))
  expect(remove).not.toHaveBeenCalled()
  expect(await screen.findByText('replacement question')).toBeInTheDocument()
  expect(screen.getByText('openai / gpt · User')).toBeInTheDocument()
})
