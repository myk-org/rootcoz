import { beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { api } from '@/lib/api'
import { ServerProvidersCard } from './ServerProvidersCard'

Element.prototype.scrollIntoView = vi.fn()

vi.mock('@/lib/api', () => ({
  api: { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() },
  extractApiDetail: () => null,
}))
vi.mock('@/lib/useProviderOptions', () => ({
  resetProviderCatalogCache: vi.fn(),
  useProviderCatalog: () => ({
    providers: {
      gemini: [
        { id: 'gemini-2.5-pro', name: 'Gemini 2.5 Pro' },
        { id: 'gemini-2.5-flash', name: 'Flash' },
      ],
      openai: [{ id: 'gpt-4o', name: 'GPT-4o' }],
    },
    providerStatus: {},
  }),
}))

const get = vi.mocked(api.get)
const post = vi.mocked(api.post)
const put = vi.mocked(api.put)
const del = vi.mocked(api.delete)

type Model = { id: string; name?: string; included: boolean; free: boolean }
type Provider = { provider: string; verified: boolean; models: Model[] }

function providersState(): Provider[] {
  return [
    {
      provider: 'gemini',
      verified: true,
      models: [
        { id: 'gemini-2.5-pro', name: 'Gemini 2.5 Pro', included: true, free: false },
        { id: 'gemini-2.5-flash', name: 'Flash', included: false, free: false },
        { id: 'gemini-2.0', name: '', included: false, free: false },
      ],
    },
  ]
}

beforeEach(() => {
  vi.clearAllMocks()
  get.mockResolvedValue(providersState() as never)
  post.mockResolvedValue({} as never)
  put.mockResolvedValue({} as never)
  del.mockResolvedValue({} as never)
})

describe('ServerProvidersCard add flow gate', () => {
  it('keeps Add disabled until the provider test passes, then finishes the add', async () => {
    const user = userEvent.setup()
    get.mockResolvedValue([] as never)
    render(<ServerProvidersCard />)

    await user.click(await screen.findByRole('combobox', { name: 'Provider' }))
    await user.click(await screen.findByRole('option', { name: 'openai' }))
    await user.click(screen.getByRole('combobox', { name: 'Verification model' }))
    await user.click(await screen.findByRole('option', { name: /gpt-4o/ }))
    await user.type(screen.getByLabelText('API key'), 'sk-test')

    const addButton = screen.getByRole('button', { name: '+ Add provider' })
    expect(addButton).toBeDisabled()
    expect(post).not.toHaveBeenCalled()

    await user.click(screen.getByRole('button', { name: 'Test provider' }))
    await waitFor(() =>
      expect(post).toHaveBeenCalledWith('/api/admin/server-providers', {
        provider: 'openai',
        api_key: 'sk-test', // pragma: allowlist secret
        verification_model: 'gpt-4o',
      }),
    )
    expect(await screen.findByRole('status')).toHaveTextContent('Provider verified')
    expect(addButton).toBeEnabled()

    await user.click(addButton)
    expect(await screen.findByRole('status')).toHaveTextContent('Server provider added.')
    // Form reset: the provider picker starts empty again
    expect(screen.getByRole('combobox', { name: 'Provider' })).toHaveValue('')
  })

  it('keeps Add disabled and clears the key when the test fails', async () => {
    const user = userEvent.setup()
    get.mockResolvedValue([] as never)
    post.mockRejectedValueOnce(new Error('verification failed') as never)
    render(<ServerProvidersCard />)

    await user.click(await screen.findByRole('combobox', { name: 'Provider' }))
    await user.click(await screen.findByRole('option', { name: 'openai' }))
    await user.type(screen.getByRole('combobox', { name: 'Verification model' }), 'manual-model')
    await user.type(screen.getByLabelText('API key'), 'sk-bad')

    await user.click(screen.getByRole('button', { name: 'Test provider' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Provider verification failed')
    expect(screen.getByRole('button', { name: '+ Add provider' })).toBeDisabled()
    expect(screen.getByLabelText('API key')).toHaveValue('')
  })

  it('invalidates a passed test when the key changes', async () => {
    const user = userEvent.setup()
    get.mockResolvedValue([] as never)
    render(<ServerProvidersCard />)

    await user.click(await screen.findByRole('combobox', { name: 'Provider' }))
    await user.click(await screen.findByRole('option', { name: 'openai' }))
    await user.type(screen.getByRole('combobox', { name: 'Verification model' }), 'manual-model')
    await user.type(screen.getByLabelText('API key'), 'sk-test')
    await user.click(screen.getByRole('button', { name: 'Test provider' }))
    await waitFor(() => expect(screen.getByRole('button', { name: '+ Add provider' })).toBeEnabled())

    await user.type(screen.getByLabelText('API key'), '2')
    expect(screen.getByRole('button', { name: '+ Add provider' })).toBeDisabled()
  })
})

describe('ServerProvidersCard model multi-select', () => {
  async function openModels(user: ReturnType<typeof userEvent.setup>) {
    render(<ServerProvidersCard />)
    await screen.findByRole('list', { name: 'Configured server providers' })
    await user.click(screen.getByRole('button', { name: 'gemini models' }))
    expect(await screen.findByRole('checkbox', { name: 'Select all' })).toBeInTheDocument()
  }

  it('shows per-model checkmarks, Select all with an indeterminate partial state, and saves', async () => {
    const user = userEvent.setup()
    await openModels(user)

    expect(screen.getByRole('checkbox', { name: 'gemini-2.5-pro' })).toBeChecked()
    expect(screen.getByRole('checkbox', { name: 'gemini-2.5-flash' })).not.toBeChecked()

    // Partial selection → Select all is indeterminate
    const selectAll = screen.getByRole('checkbox', { name: 'Select all' }) as HTMLInputElement
    expect(selectAll).not.toBeChecked()
    expect(selectAll.indeterminate).toBe(true)

    await user.click(selectAll)
    await waitFor(() =>
      expect(put).toHaveBeenCalledWith('/api/admin/server-providers/gemini/models', {
        models: [
          { id: 'gemini-2.5-pro', name: 'Gemini 2.5 Pro', included: true, free: false },
          { id: 'gemini-2.5-flash', name: 'Flash', included: true, free: false },
          { id: 'gemini-2.0', name: '', included: true, free: false },
        ],
      }),
    )
    expect(await screen.findByRole('status')).toHaveTextContent('Saved')
    expect(screen.getByRole('checkbox', { name: 'Select all' })).toBeChecked()

    // Unchecking one makes Select all indeterminate again
    await user.click(screen.getByRole('checkbox', { name: 'gemini-2.0' }))
    await waitFor(() => expect(put).toHaveBeenCalledTimes(2))
    expect((screen.getByRole('checkbox', { name: 'Select all' }) as HTMLInputElement).indeterminate).toBe(true)
  })

  it('filters the model list', async () => {
    const user = userEvent.setup()
    await openModels(user)

    await user.type(screen.getByRole('textbox', { name: 'Filter models' }), 'flash')
    expect(screen.getByRole('checkbox', { name: 'gemini-2.5-flash' })).toBeInTheDocument()
    expect(screen.queryByRole('checkbox', { name: 'gemini-2.5-pro' })).not.toBeInTheDocument()
  })
})

describe('ServerProvidersCard free pill', () => {
  async function openModels(user: ReturnType<typeof userEvent.setup>) {
    render(<ServerProvidersCard />)
    await screen.findByRole('list', { name: 'Configured server providers' })
    await user.click(screen.getByRole('button', { name: 'gemini models' }))
    await screen.findByRole('checkbox', { name: 'Select all' })
  }

  it('marks a model free and auto-includes it', async () => {
    const user = userEvent.setup()
    await openModels(user)

    const pill = screen.getByRole('button', { name: 'Toggle gemini-2.5-flash free' })
    expect(pill).toHaveAttribute('aria-pressed', 'false')

    await user.click(pill)
    await waitFor(() =>
      expect(put).toHaveBeenCalledWith('/api/admin/server-providers/gemini/models', {
        models: [
          { id: 'gemini-2.5-pro', name: 'Gemini 2.5 Pro', included: true, free: false },
          { id: 'gemini-2.5-flash', name: 'Flash', included: true, free: true },
          { id: 'gemini-2.0', name: '', included: false, free: false },
        ],
      }),
    )
    expect(screen.getByRole('button', { name: 'Toggle gemini-2.5-flash free' })).toHaveAttribute('aria-pressed', 'true')
    expect(screen.getByRole('checkbox', { name: 'gemini-2.5-flash' })).toBeChecked()
    expect(await screen.findByRole('status')).toHaveTextContent('Saved')
  })

  it('clears the free flag when the model is un-included', async () => {
    const user = userEvent.setup()
    const state = providersState()
    state[0].models[1] = { id: 'gemini-2.5-flash', name: 'Flash', included: true, free: true }
    get.mockResolvedValue(state as never)
    await openModels(user)

    expect(screen.getByRole('button', { name: 'Toggle gemini-2.5-flash free' })).toHaveAttribute('aria-pressed', 'true')

    await user.click(screen.getByRole('checkbox', { name: 'gemini-2.5-flash' }))
    await waitFor(() =>
      expect(put).toHaveBeenCalledWith('/api/admin/server-providers/gemini/models', {
        models: [
          { id: 'gemini-2.5-pro', name: 'Gemini 2.5 Pro', included: true, free: false },
          { id: 'gemini-2.5-flash', name: 'Flash', included: false, free: false },
          { id: 'gemini-2.0', name: '', included: false, free: false },
        ],
      }),
    )
    expect(screen.getByRole('button', { name: 'Toggle gemini-2.5-flash free' })).toHaveAttribute('aria-pressed', 'false')
  })
})

describe('ServerProvidersCard remove and rotate', () => {
  it('confirms removal with the affected models from the delete response warning', async () => {
    const user = userEvent.setup()
    let removed = false
    get.mockImplementation(async () => (removed ? [] : providersState()) as never)
    del.mockImplementation(async (_url: string, body?: unknown) => {
      const confirmed = (body as { confirm?: boolean } | undefined)?.confirm === true
      if (confirmed) removed = true
      return confirmed
        ? {}
        : { warning: { models: ['gemini-2.5-pro'], free: ['gemini-2.5-flash'] } }
    })

    render(<ServerProvidersCard />)
    await user.click(await screen.findByRole('button', { name: 'Remove gemini' }))

    // The confirm dialog shows the affected selected and free models
    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).getByText(/gemini-2\.5-pro/)).toBeInTheDocument()
    expect(within(dialog).getByText(/gemini-2\.5-flash/)).toBeInTheDocument()

    await user.click(within(dialog).getByRole('button', { name: 'Remove provider' }))
    await waitFor(() =>
      expect(del).toHaveBeenLastCalledWith('/api/admin/server-providers/gemini', { confirm: true }),
    )
    await waitFor(() =>
      expect(screen.queryByRole('list', { name: 'Configured server providers' })).not.toBeInTheDocument(),
    )
    expect(await screen.findByRole('status')).toHaveTextContent('gemini removed.')
  })

  it('removes without a confirm when no models are affected', async () => {
    const user = userEvent.setup()
    let removed = false
    get.mockImplementation(async () => (removed ? [] : providersState()) as never)
    del.mockImplementation(async () => {
      removed = true
      return {}
    })

    render(<ServerProvidersCard />)
    await user.click(await screen.findByRole('button', { name: 'Remove gemini' }))

    await waitFor(() =>
      expect(screen.queryByRole('list', { name: 'Configured server providers' })).not.toBeInTheDocument(),
    )
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(del).toHaveBeenCalledTimes(1)
  })

  it('rotates a key via the rotate endpoint', async () => {
    const user = userEvent.setup()
    render(<ServerProvidersCard />)
    await user.click(await screen.findByRole('button', { name: 'Rotate gemini key' }))

    await user.type(screen.getByRole('combobox', { name: 'Verification model' }), 'manual-model')
    await user.type(screen.getByLabelText('API key'), 'new-secret')
    await user.click(screen.getByRole('button', { name: 'Test provider' }))

    await waitFor(() =>
      expect(post).toHaveBeenCalledWith('/api/admin/server-providers/gemini/rotate', {
        api_key: 'new-secret', // pragma: allowlist secret
        verification_model: 'manual-model',
      }),
    )
    expect(await screen.findByRole('status')).toHaveTextContent('API key verified and rotated.')
  })
})
