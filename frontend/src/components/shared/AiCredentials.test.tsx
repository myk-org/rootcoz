import { beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { api } from '@/lib/api'
import { AiCredentials } from './AiCredentials'

Element.prototype.scrollIntoView = vi.fn()

vi.mock('@/lib/api', () => ({ api: { get: vi.fn(), put: vi.fn(), delete: vi.fn() } }))

const get = vi.mocked(api.get)
const put = vi.mocked(api.put)
const remove = vi.mocked(api.delete)

beforeEach(() => {
  vi.resetAllMocks()
  get.mockResolvedValue({ providers: [{ provider: 'openai/custom', configured: false }, { provider: 'other', configured: false }] })
  put.mockResolvedValue(undefined)
  remove.mockResolvedValue(undefined)
})

describe('AiCredentials', () => {
  it('discovers providers, searches, adds two keys, and removes just one', async () => {
    const user = userEvent.setup()
    render(<AiCredentials />)
    const picker = await screen.findByRole('combobox', { name: 'AI provider' })
    expect(screen.queryByText('anthropic')).not.toBeInTheDocument()
    await user.type(picker, 'custom')
    await user.click(await screen.findByRole('option', { name: 'openai/custom' }))
    await user.type(screen.getByLabelText('API key'), 'first-secret')
    await user.click(screen.getByRole('button', { name: '+ Add key' }))
    await waitFor(() => expect(put).toHaveBeenCalledWith('/api/user/ai-credentials/openai%2Fcustom', { api_key: 'first-secret' })) // pragma: allowlist secret
    expect(await screen.findByText('openai/custom')).toBeInTheDocument()
    expect(screen.getByLabelText('API key')).toHaveValue('')

    await user.click(picker)
    expect(screen.queryByRole('option', { name: 'openai/custom' })).not.toBeInTheDocument()
    await user.click(await screen.findByRole('option', { name: 'other' }))
    await user.type(screen.getByLabelText('API key'), 'second-secret')
    await user.click(screen.getByRole('button', { name: '+ Add key' }))
    await waitFor(() => expect(put).toHaveBeenCalledWith('/api/user/ai-credentials/other', { api_key: 'second-secret' })) // pragma: allowlist secret
    expect(screen.getAllByText('Configured')).toHaveLength(2)
    await user.click(screen.getByRole('button', { name: 'Remove openai/custom key' }))
    await waitFor(() => expect(remove).toHaveBeenCalledWith('/api/user/ai-credentials/openai%2Fcustom'))
    expect(within(screen.getByRole('list', { name: 'Configured AI providers' })).queryByText('openai/custom')).not.toBeInTheDocument()
    expect(screen.getByText('other')).toBeInTheDocument()
    expect(screen.getAllByText('Configured')).toHaveLength(1)
  })

  it('does not submit unsupported free text', async () => {
    const user = userEvent.setup()
    render(<AiCredentials />)
    const picker = await screen.findByRole('combobox', { name: 'AI provider' })
    await user.type(picker, 'unsupported')
    await user.type(screen.getByLabelText('API key'), 'secret-key')
    expect(screen.getByRole('button', { name: '+ Add key' })).toBeDisabled()
    expect(put).not.toHaveBeenCalled()
  })

  it('clears the key after a failed save and permits a retry', async () => {
    const user = userEvent.setup()
    put.mockRejectedValueOnce(new Error('failed'))
    render(<AiCredentials />)
    await user.click(await screen.findByRole('combobox', { name: 'AI provider' }))
    await user.click(await screen.findByRole('option', { name: 'other' }))
    const input = screen.getByLabelText('API key')
    await user.type(input, 'secret-key')
    await user.click(screen.getByRole('button', { name: '+ Add key' }))
    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('Could not save API key'))
    expect(input).toHaveValue('')
    expect(screen.queryByText('Configured')).not.toBeInTheDocument()
    await user.type(input, 'new-key')
    await user.click(screen.getByRole('button', { name: '+ Add key' }))
    await waitFor(() => expect(put).toHaveBeenCalledTimes(2))
  })

  it('clears the key and locks controls while saving', async () => {
    const user = userEvent.setup()
    let resolve!: (value: unknown) => void
    put.mockImplementationOnce(() => new Promise((done) => { resolve = done }))
    render(<AiCredentials />)
    await user.click(await screen.findByRole('combobox', { name: 'AI provider' }))
    await user.click(await screen.findByRole('option', { name: 'other' }))
    await user.type(screen.getByLabelText('API key'), 'transient-secret')
    await user.click(screen.getByRole('button', { name: '+ Add key' }))
    expect(screen.getByLabelText('API key')).toHaveValue('')
    expect(screen.getByRole('combobox', { name: 'AI provider' })).toBeDisabled()
    expect(screen.getByRole('button', { name: '+ Add key' })).toBeDisabled()
    resolve(undefined)
    await waitFor(() => expect(screen.getByText('Configured')).toBeInTheDocument())
  })

  it('replaces a configured key without removing it first', async () => {
    const user = userEvent.setup()
    get.mockResolvedValueOnce({ providers: [{ provider: 'other', configured: true }] })
    render(<AiCredentials />)
    await user.click(await screen.findByRole('button', { name: 'Replace other key' }))
    await user.type(screen.getByLabelText('API key'), 'replacement')
    await user.click(screen.getByRole('button', { name: 'Replace key' }))
    await waitFor(() => expect(put).toHaveBeenCalledWith('/api/user/ai-credentials/other', { api_key: 'replacement' })) // pragma: allowlist secret
    expect(remove).not.toHaveBeenCalled()
    expect(screen.getByText('Configured')).toBeInTheDocument()
  })

  it('does not show controls when loading fails', async () => {
    get.mockRejectedValueOnce(new Error('failed'))
    render(<AiCredentials />)
    expect(await screen.findByRole('alert')).toHaveTextContent('Could not load AI credentials')
    expect(screen.queryByRole('combobox')).not.toBeInTheDocument()
    expect(screen.queryByRole('textbox')).not.toBeInTheDocument()
  })
})
