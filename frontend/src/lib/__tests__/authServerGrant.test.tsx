import { act, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { AuthProvider, useAuth } from '@/lib/auth'
import { ApiError } from '@/lib/api'
import { getUsername, setUsername, clearUsername } from '@/lib/cookies'
import * as catalog from '@/lib/useProviderOptions'

const get = vi.fn()
const post = vi.fn()
vi.mock('@/lib/api', async (importActual) => ({
  ...await importActual<typeof import('@/lib/api')>(),
  api: { get: (...args: unknown[]) => get(...args), post: (...args: unknown[]) => post(...args) },
}))

function Grant() {
  const { canUseServerProviders } = useAuth()
  return <span>{canUseServerProviders ? 'Allowed' : 'Denied'}</span>
}

afterEach(() => { get.mockReset(); post.mockReset(); clearUsername(); vi.restoreAllMocks() })

it('treats admins as granted despite a false stored flag, but not ungranted non-admins', async () => {
  get.mockImplementation(async (path: string) => path === '/api/auth/me'
    ? { username: 'admin2', role: 'admin', is_admin: true, can_view_reports: true, can_use_server_providers: false }
    : { github_token: '', jira_email: '', jira_token: '' })
  const { unmount } = render(<AuthProvider><Grant /></AuthProvider>)
  await waitFor(() => expect(screen.getByText('Allowed')).toBeInTheDocument())
  unmount()
  get.mockImplementation(async (path: string) => path === '/api/auth/me'
    ? { username: 'alice', role: 'reviewer', is_admin: false, can_view_reports: false, can_use_server_providers: false }
    : { github_token: '', jira_email: '', jira_token: '' })
  render(<AuthProvider><Grant /></AuthProvider>)
  await waitFor(() => expect(get).toHaveBeenCalledWith('/api/user/tokens'))
  expect(screen.getByText('Denied')).toBeInTheDocument()
})

it('does not let a pending refresh overwrite a new login', async () => {
  let resolveOld!: (value: unknown) => void
  let meCalls = 0
  get.mockImplementation((path: string) => path === '/api/auth/me'
    ? ++meCalls === 1 ? new Promise((resolve) => { resolveOld = resolve }) : new Promise(() => {})
    : Promise.resolve({ github_token: '', jira_email: '', jira_token: '' }))
  post.mockResolvedValue({ username: 'bob', role: 'reviewer', is_admin: false, can_use_server_providers: false })
  function Login() {
    const { login, username, canUseServerProviders } = useAuth()
    return <button onClick={() => void login('bob', 'key')}>{username}:{canUseServerProviders ? 'Allowed' : 'Denied'}</button>
  }
  render(<AuthProvider><Login /></AuthProvider>)
  await waitFor(() => expect(resolveOld).toBeDefined())
  await act(async () => { screen.getByRole('button').click() })
  await waitFor(() => expect(screen.getByRole('button', { name: 'bob:Denied' })).toBeInTheDocument())
  await act(async () => { resolveOld({ username: 'alice', role: 'admin', is_admin: true, can_use_server_providers: true }) })
  expect(screen.getByRole('button', { name: 'bob:Denied' })).toBeInTheDocument()
})

it.each([['login', false], ['logout', false], ['logout', true]] as const)('ignores an old provider %s response after unmount (rejected: %s)', async (operation, rejected) => {
  setUsername('old')
  const reset = vi.spyOn(catalog, 'resetProviderCatalogCache')
  let resolvePost!: (value: unknown) => void
  let rejectPost!: (reason: Error) => void
  let pending!: Promise<void>
  get.mockImplementation(async (path: string) => path === '/api/auth/me'
    ? { username: getUsername(), role: 'reviewer', is_admin: false, can_use_server_providers: false }
    : { github_token: '', jira_email: '', jira_token: '' })
  post.mockImplementation(() => new Promise((resolve, reject) => { resolvePost = resolve; rejectPost = reject }))
  function Controls() {
    const auth = useAuth()
    return <button onClick={() => { pending = operation === 'login' ? auth.login('stale', 'key') : auth.logout() }}>
      {auth.username}:{auth.authenticated ? 'yes' : 'no'}:{auth.loading ? 'loading' : 'ready'}
    </button>
  }
  const old = render(<AuthProvider><Controls /></AuthProvider>)
  await waitFor(() => expect(screen.getByRole('button', { name: 'old:yes:ready' })).toBeInTheDocument())
  await act(async () => { screen.getByRole('button').click() })
  await waitFor(() => expect(post).toHaveBeenCalled())
  old.unmount()
  setUsername('new')
  render(<AuthProvider><Controls /></AuthProvider>)
  await waitFor(() => expect(screen.getByRole('button', { name: 'new:yes:ready' })).toBeInTheDocument())
  reset.mockClear()
  await act(async () => {
    if (rejected) rejectPost(new Error('network error'))
    else resolvePost({ username: 'stale', role: 'admin', is_admin: true, can_use_server_providers: true })
    await pending
  })
  expect(getUsername()).toBe('new')
  expect(localStorage.getItem('rootcoz_is_admin')).toBeNull()
  expect(screen.getByRole('button', { name: 'new:yes:ready' })).toBeInTheDocument()
  expect(reset).not.toHaveBeenCalled()
})

it('ignores a deferred post-login /me response after unmount', async () => {
  let resolveMe!: (value: unknown) => void
  let meCalls = 0
  get.mockImplementation((path: string) => path === '/api/auth/me'
    ? ++meCalls === 2 ? new Promise((resolve) => { resolveMe = resolve })
      : Promise.resolve({ username: meCalls === 1 ? 'old' : 'new', role: 'reviewer', is_admin: false })
    : Promise.resolve({ github_token: '', jira_email: '', jira_token: '' }))
  post.mockResolvedValue({ username: 'old', role: 'reviewer', is_admin: false })
  function Controls() {
    const { login, username } = useAuth()
    return <button onClick={() => void login('old', 'key')}>{username}</button>
  }
  const old = render(<AuthProvider><Controls /></AuthProvider>)
  await waitFor(() => expect(screen.getByRole('button', { name: 'old' })).toBeInTheDocument())
  await act(async () => { screen.getByRole('button').click() })
  await waitFor(() => expect(resolveMe).toBeDefined())
  old.unmount()
  setUsername('new')
  render(<AuthProvider><Controls /></AuthProvider>)
  await waitFor(() => expect(screen.getByRole('button', { name: 'new' })).toBeInTheDocument())
  await act(async () => { resolveMe({ username: 'stale', role: 'admin', is_admin: true }) })
  expect(getUsername()).toBe('new')
  expect(localStorage.getItem('rootcoz_is_admin')).toBeNull()
  expect(screen.getByRole('button', { name: 'new' })).toBeInTheDocument()
})

it('ignores an old provider refresh rejection after unmount', async () => {
  setUsername('old')
  let rejectMe!: (reason: Error) => void
  get.mockImplementationOnce(() => new Promise((_resolve, reject) => { rejectMe = reject }))
    .mockImplementation(async (path: string) => path === '/api/auth/me'
      ? { username: 'new', role: 'reviewer', is_admin: false, can_use_server_providers: false }
      : { github_token: '', jira_email: '', jira_token: '' })
  const old = render(<AuthProvider><Grant /></AuthProvider>)
  old.unmount()
  setUsername('new')
  render(<AuthProvider><Grant /></AuthProvider>)
  await waitFor(() => expect(screen.getByText('Denied')).toBeInTheDocument())
  await act(async () => { rejectMe(new ApiError(401, 'unauthorized', null)) })
  expect(getUsername()).toBe('new')
})

it.each(['login', 'logout'] as const)('defers focus refresh until a pending %s settles', async (operation) => {
  let resolvePost!: (value: unknown) => void
  const oldUser = { username: 'alice', role: 'reviewer', is_admin: false, can_use_server_providers: false }
  const newUser = { username: 'bob', role: 'reviewer', is_admin: false, can_use_server_providers: true }
  let serverUser: typeof oldUser | null = oldUser
  get.mockImplementation(async (path: string) => path === '/api/auth/me'
    ? serverUser ?? Promise.reject(new ApiError(401, 'unauthorized', null))
    : { github_token: '', jira_email: '', jira_token: '' })
  post.mockImplementation(() => new Promise((resolve) => { resolvePost = resolve }))
  function Controls() {
    const auth = useAuth()
    return <button onClick={() => void (operation === 'login' ? auth.login('bob', 'key') : auth.logout())}>
      {auth.username}:{auth.authenticated ? 'yes' : 'no'}:{auth.canUseServerProviders ? 'Allowed' : 'Denied'}
    </button>
  }
  render(<AuthProvider><Controls /></AuthProvider>)
  await waitFor(() => expect(screen.getByRole('button', { name: 'alice:yes:Denied' })).toBeInTheDocument())
  act(() => screen.getByRole('button').click())
  await waitFor(() => expect(resolvePost).toBeDefined())
  const meCalls = get.mock.calls.filter(([path]) => path === '/api/auth/me').length
  act(() => window.dispatchEvent(new Event('focus')))
  expect(get.mock.calls.filter(([path]) => path === '/api/auth/me')).toHaveLength(meCalls)
  await act(async () => {
    serverUser = operation === 'login' ? newUser : null
    resolvePost(operation === 'login' ? newUser : {})
  })
  await waitFor(() => expect(get.mock.calls.filter(([path]) => path === '/api/auth/me').length).toBeGreaterThan(meCalls))
  expect(screen.getByRole('button', { name: operation === 'login' ? 'bob:yes:Allowed' : ':no:Denied' })).toBeInTheDocument()
})

it('reconciles after an older overlapping login completes after logout', async () => {
  let resolveLogin!: (value: unknown) => void
  let serverUser = { username: 'alice', role: 'reviewer', is_admin: false, can_use_server_providers: false }
  get.mockImplementation(async (path: string) => path === '/api/auth/me'
    ? serverUser : { github_token: '', jira_email: '', jira_token: '' })
  post.mockImplementation((path: string) => path === '/api/auth/login'
    ? new Promise((resolve) => { resolveLogin = resolve }) : Promise.resolve({}))
  function Controls() {
    const auth = useAuth()
    return <><span>{auth.username}:{auth.authenticated ? 'yes' : 'no'}</span>
      <button onClick={() => void auth.login('bob', 'key')}>Login</button>
      <button onClick={() => void auth.logout()}>Logout</button></>
  }
  render(<AuthProvider><Controls /></AuthProvider>)
  await waitFor(() => expect(screen.getByText('alice:yes')).toBeInTheDocument())
  act(() => screen.getByRole('button', { name: 'Login' }).click())
  await waitFor(() => expect(resolveLogin).toBeDefined())
  act(() => screen.getByRole('button', { name: 'Logout' }).click())
  await waitFor(() => expect(screen.getByText(':no')).toBeInTheDocument())
  await act(async () => {
    serverUser = { username: 'bob', role: 'reviewer', is_admin: false, can_use_server_providers: true }
    resolveLogin(serverUser)
  })
  await waitFor(() => expect(screen.getByText('bob:yes')).toBeInTheDocument())
})

it('reconciles after an older overlapping logout completes after login', async () => {
  let resolveLogout!: (value: unknown) => void
  const alice = { username: 'alice', role: 'reviewer', is_admin: false, can_use_server_providers: false }
  const bob = { username: 'bob', role: 'reviewer', is_admin: false, can_use_server_providers: true }
  let serverUser: typeof alice | null = alice
  get.mockImplementation(async (path: string) => path === '/api/auth/me'
    ? serverUser ?? Promise.reject(new ApiError(401, 'unauthorized', null))
    : { github_token: '', jira_email: '', jira_token: '' })
  post.mockImplementation((path: string) => path === '/api/auth/logout'
    ? new Promise((resolve) => { resolveLogout = resolve }) : Promise.resolve(bob))
  function Controls() {
    const auth = useAuth()
    return <><span>{auth.username}:{auth.authenticated ? 'yes' : 'no'}</span>
      <button onClick={() => void auth.logout()}>Logout</button>
      <button onClick={() => void auth.login('bob', 'key')}>Login</button></>
  }
  render(<AuthProvider><Controls /></AuthProvider>)
  await waitFor(() => expect(screen.getByText('alice:yes')).toBeInTheDocument())
  act(() => screen.getByRole('button', { name: 'Logout' }).click())
  await waitFor(() => expect(resolveLogout).toBeDefined())
  serverUser = bob
  await act(async () => { screen.getByRole('button', { name: 'Login' }).click() })
  await waitFor(() => expect(screen.getByText('bob:yes')).toBeInTheDocument())
  await act(async () => { serverUser = null; resolveLogout({}) })
  await waitFor(() => expect(screen.getByText(':no')).toBeInTheDocument())
})

it('does not trust a display cookie after an initial network failure', async () => {
  setUsername('alice')
  get.mockRejectedValue(new Error('offline'))
  function Status() {
    const { username, authenticated, canUseServerProviders, loading } = useAuth()
    return <span>{username}:{authenticated ? 'yes' : 'no'}:{canUseServerProviders ? 'Allowed' : 'Denied'}:{loading ? 'loading' : 'ready'}</span>
  }
  render(<AuthProvider><Status /></AuthProvider>)
  await waitFor(() => expect(screen.getByText('alice:no:Denied:ready')).toBeInTheDocument())
})

it('does not let a pending refresh restore a grant after logout', async () => {
  let resolveMe!: (value: unknown) => void
  get.mockImplementation((path: string) => path === '/api/auth/me'
    ? new Promise((resolve) => { resolveMe = resolve })
    : Promise.resolve({ github_token: '', jira_email: '', jira_token: '' }))
  post.mockResolvedValue({})
  function Logout() {
    const { logout, canUseServerProviders } = useAuth()
    return <button onClick={() => void logout()}>{canUseServerProviders ? 'Allowed' : 'Denied'}</button>
  }
  render(<AuthProvider><Logout /></AuthProvider>)
  await waitFor(() => expect(resolveMe).toBeDefined())
  await act(async () => { screen.getByRole('button').click() })
  await waitFor(() => expect(post).toHaveBeenCalledWith('/api/auth/logout'))
  await act(async () => { resolveMe({ username: 'alice', role: 'reviewer', is_admin: false, can_use_server_providers: true }) })
  expect(screen.getByRole('button', { name: 'Denied' })).toBeInTheDocument()
})
