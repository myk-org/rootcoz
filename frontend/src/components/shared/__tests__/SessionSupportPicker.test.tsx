import { cleanup, render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, expect, it, vi } from 'vitest'
import { AnalysisModelSelect } from '@/components/shared/AnalysisAiPicker'
import { ChatUI } from '@/components/shared/ChatUI'
import { resetProviderCatalogCache } from '@/lib/useProviderOptions'

/** The picker reason the backend attaches to an unsessionable pair. */
const REASON =
  "AI session refused: this model is listed by the provider but is not declared in that provider's models[] and the listing reports no token limits. Declare it in the agent-dir models.json models[] — limits are optional there, pi's defaults apply — or pick a model that is declared or lists both limits."

const catalog = {
  providers: {
    enmaas: [
      {
        id: 'rits/zai-org/glm-5-3',
        name: 'GLM 5.3',
        provider: 'enmaas',
        credential_sources: ['server'],
        session_supported: true,
      },
      {
        id: 'gemini-3.1-pro-preview',
        name: 'Gemini 3.1 Pro',
        provider: 'enmaas',
        credential_sources: ['server'],
        session_supported: false,
        session_unsupported_reason: REASON,
      },
    ],
  },
  provider_status: {},
}

const post = vi.fn()
vi.mock('@/lib/api', async importOriginal => ({
  ...(await importOriginal<typeof import('@/lib/api')>()),
  api: {
    get: async (path: string) =>
      path.startsWith('/api/ai-models')
        ? catalog
        : path.startsWith('/api/chat')
          ? { messages: [], total: 0, active_session_version: 'no-session-version' }
          : {},
    post: (...args: unknown[]) => (post(...args) ?? Promise.resolve({})),
    delete: vi.fn().mockResolvedValue({}),
  },
}))
vi.mock('@/lib/auth', () => ({
  useAuth: () => ({ username: 'alice', isAdmin: true, authenticated: true, canUseServerProviders: true, role: 'admin' }),
}))
vi.mock('@/lib/useSSE', () => ({ useSSE: vi.fn() }))

HTMLElement.prototype.hasPointerCapture = () => false
HTMLElement.prototype.setPointerCapture = () => {}
HTMLElement.prototype.releasePointerCapture = () => {}
HTMLElement.prototype.scrollIntoView = () => {}

afterEach(() => {
  cleanup()
  post.mockReset()
  resetProviderCatalogCache()
})

/** Both entry points must refuse the same pair: the flag lives in the shared select. */
const entryPoints = {
  'analysis picker': () =>
    render(<AnalysisModelSelect provider="enmaas" value="" onChange={() => {}} forceServer={false} />),
  'job chat': () =>
    render(<ChatUI apiBasePath="/api/chat/job" sseTopic="chat:job" header={<h1>Job chat</h1>} defaultProvider="enmaas" defaultModel="rits/zai-org/glm-5-3" />),
}

for (const [name, renderPicker] of Object.entries(entryPoints)) {
  it(`refuses an unsessionable model at the ${name}`, async () => {
    const user = userEvent.setup()
    renderPicker()
    const input = await screen.findByRole('combobox', { name: 'AI Model' })
    await user.click(input)
    const blocked = await screen.findByRole('option', { name: /gemini-3\.1-pro-preview/ })
    const allowed = screen.getByRole('option', { name: /glm-5-3/ })

    // The neighbour in the same list stays selectable, so "no selection" cannot
    // pass as "the list is broken".
    expect(allowed).not.toHaveAttribute('aria-disabled')
    expect(blocked).toHaveAttribute('aria-disabled', 'true')
    expect(within(blocked).getByText('NO SESSION')).toBeInTheDocument()

    // The reason, not a bare "unavailable": it names the manifest fix.
    await user.hover(blocked)
    expect(await screen.findByRole('tooltip')).toHaveTextContent('models.json')
    await user.unhover(blocked)

    const before = (input as HTMLInputElement).value
    await user.click(blocked)
    expect((input as HTMLInputElement).value).toBe(before)

    // Keyboard must not be a way around it either.
    await user.click(input)
    await user.keyboard('{ArrowDown}{ArrowDown}{ArrowDown}{Enter}')
    expect((input as HTMLInputElement).value).not.toBe('gemini-3.1-pro-preview')
  })
}
