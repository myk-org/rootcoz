import { beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { api } from '@/lib/api'
import { AiCredentials } from './AiCredentials'

vi.mock('@/lib/api', () => ({ api: { get: vi.fn(), put: vi.fn(), delete: vi.fn() } }))

const get = vi.mocked(api.get)
const put = vi.mocked(api.put)
const remove = vi.mocked(api.delete)

beforeEach(() => {
  vi.resetAllMocks()
  get.mockResolvedValue({ providers: [{ provider: 'openai/custom', configured: false }, { provider: 'other', configured: true }] })
  put.mockResolvedValue(undefined)
  remove.mockResolvedValue(undefined)
})

describe('AiCredentials', () => {
  it('shows only server-listed providers and saves a transient key with an encoded URL', async () => {
    const user = userEvent.setup()
    render(<AiCredentials />)
    const input = await screen.findByLabelText('openai/custom API key')
    expect(screen.getByText('other')).toBeInTheDocument()
    expect(screen.queryByText('anthropic')).not.toBeInTheDocument()
    await user.type(input, 'secret-key')
    await user.click(screen.getByRole('button', { name: 'Save openai/custom key' }))
    await waitFor(() => expect(put).toHaveBeenCalledWith('/api/user/ai-credentials/openai%2Fcustom', { api_key: 'secret-key' })) // pragma: allowlist secret
    expect(input).toHaveValue('')
    await waitFor(() => expect(screen.getAllByText('Configured')).toHaveLength(2))
  })

  it('clears the key on failure and removes a configured key', async () => {
    const user = userEvent.setup()
    put.mockRejectedValueOnce(new Error('failed'))
    render(<AiCredentials />)
    const input = await screen.findByLabelText('openai/custom API key')
    await user.type(input, 'secret-key')
    await user.click(screen.getByRole('button', { name: 'Save openai/custom key' }))
    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('Could not save API key'))
    expect(input).toHaveValue('')
    await user.click(screen.getByRole('button', { name: 'Remove other key' }))
    await waitFor(() => expect(remove).toHaveBeenCalledWith('/api/user/ai-credentials/other'))
    expect(screen.getAllByText('Not configured')).toHaveLength(2)
  })

  it('does not show controls when loading fails', async () => {
    get.mockRejectedValueOnce(new Error('failed'))
    render(<AiCredentials />)
    expect(await screen.findByRole('alert')).toHaveTextContent('Could not load AI credentials')
    expect(screen.queryByRole('textbox')).not.toBeInTheDocument()
  })
})
