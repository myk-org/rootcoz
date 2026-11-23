import { act, cleanup, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, expect, it, vi } from 'vitest'
import { ChatPage } from '@/pages/ChatPage'
import { AdminChatPage } from '@/pages/AdminChatPage'
import { resetProviderCatalogCache } from '@/lib/providerCatalog'

const get = vi.fn()
const post = vi.fn().mockResolvedValue({})
vi.mock('@/lib/api', () => ({ api: { get: (...args: unknown[]) => get(...args), post: (...args: unknown[]) => post(...args), delete: vi.fn() } }))
vi.mock('@/lib/auth', () => ({ useAuth: () => ({ username: 'alice', isAdmin: true, authenticated: true, canUseServerProviders: true }) }))
vi.mock('@/lib/useSSE', () => ({ useSSE: vi.fn() }))
HTMLElement.prototype.scrollIntoView = () => {}
afterEach(() => { cleanup(); act(() => resetProviderCatalogCache()); get.mockReset(); post.mockClear() })

it('prefills job analysis defaults without starting chat', async () => {
  get.mockImplementation(async (path: string) => path === '/results/job' ? { result: { job_name: 'job', ai_provider: 'openai', ai_model: 'gpt' } }
    : path.startsWith('/api/ai-models') ? { providers: { openai: [{ id: 'gpt', provider: 'openai', name: 'GPT', credential_sources: ['user'] }] } }
      : { messages: [], total: 0 })
  render(<MemoryRouter initialEntries={['/chat/job']}><Routes><Route path="/chat/:jobId" element={<ChatPage />} /></Routes></MemoryRouter>)
  expect(await screen.findByRole('button', { name: 'Start Chat' })).toBeInTheDocument()
  await waitFor(() => expect(screen.getByRole('combobox', { name: 'AI Model' })).toHaveValue('gpt'))
  expect(post).not.toHaveBeenCalledWith('/api/chat/job/init', expect.anything())
})

it('prefills admin defaults without starting chat', async () => {
  get.mockImplementation(async (path: string) => path === '/api/default-server-settings' ? { ai_provider: 'openai', ai_model: 'gpt' }
    : path.startsWith('/api/ai-models') ? { providers: { openai: [{ id: 'gpt', provider: 'openai', name: 'GPT', credential_sources: ['server'] }] } }
      : { messages: [], total: 0 })
  render(<AdminChatPage />)
  expect(await screen.findByRole('button', { name: 'Start Chat' })).toBeInTheDocument()
  await waitFor(() => expect(screen.getByRole('combobox', { name: 'AI Model' })).toHaveValue('gpt'))
  expect(screen.queryByRole('switch', { name: 'Use server credentials' })).not.toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Start Chat' })).toBeEnabled()
  expect(post).not.toHaveBeenCalledWith('/api/admin/chat/init', expect.anything())
})

it('renders naive UTC timestamps in local time on AdminChatPage with no attachment UI', async () => {
  const naiveUtc = '2026-11-20 14:27:56'
  const expectedLocal = new Date(Date.UTC(2026, 10, 20, 14, 27, 56)).toLocaleString()
  get.mockImplementation(async (path: string) => path === '/api/default-server-settings' ? { ai_provider: 'openai', ai_model: 'gpt' }
    : path.startsWith('/api/ai-models') ? { providers: { openai: [{ id: 'gpt', provider: 'openai', name: 'GPT', credential_sources: ['server'] }] } }
      : {
        messages: [
          { id: 1, job_id: '', role: 'assistant', content: 'admin reply', username: 'alice', ai_provider: 'openai', ai_model: 'gpt', status: 'completed', created_at: naiveUtc },
        ],
        total: 1,
        active_session: { ai_provider: 'openai', ai_model: 'gpt', credential_source: 'server' },
      })
  render(<AdminChatPage />)
  expect(await screen.findByText('admin reply')).toBeInTheDocument()
  expect(screen.getByText(expectedLocal)).toBeInTheDocument()
  expect(screen.queryByRole('button', { name: /attach files/i })).not.toBeInTheDocument()
  expect(screen.queryByText(/cannot attach files/i)).not.toBeInTheDocument()
})
