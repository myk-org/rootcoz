import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { FreeModelsCard } from '@/components/shared/FreeModelsCard'
import { ApiError } from '@/lib/api'

const get = vi.fn()
const put = vi.fn()
const resetCatalog = vi.fn()
vi.mock('@/lib/api', () => ({
  api: {
    get: (...args: unknown[]) => get(...args),
    put: (...args: unknown[]) => put(...args),
  },
  ApiError: class ApiError extends Error {
    status: number
    body: unknown
    constructor(status: number, _statusText: string, body: unknown) {
      super(`API error ${status}`)
      this.status = status
      this.body = body
    }
  },
}))
vi.mock('@/lib/useProviderOptions', () => ({
  useProviderCatalog: (forceServer: boolean) => {
    if (!forceServer) throw new Error('free pickers must use the server catalog')
    return {
      providerKeys: ['enmaas'],
      providers: {
        enmaas: [
          { id: 'glm-5-3', credential_sources: ['server'], verified: false },
          { id: 'glm-5-3-air', credential_sources: ['user'], verified: true },
        ],
      },
      enabled: ['enmaas'],
      providerStatus: {},
    }
  },
  resetProviderCatalogCache: (...args: unknown[]) => resetCatalog(...args),
}))

HTMLElement.prototype.hasPointerCapture = () => false
HTMLElement.prototype.setPointerCapture = () => {}
HTMLElement.prototype.releasePointerCapture = () => {}
HTMLElement.prototype.scrollIntoView = () => {}

afterEach(() => {
  get.mockReset()
  put.mockReset()
  resetCatalog.mockReset()
})

describe('FreeModelsCard', () => {
  it('lists declared pairs and removes them', async () => {
    get.mockResolvedValue({
      models: [{ provider: 'EnMaaS', model: 'glm-5-3' }],
      version: 'v1',
    })
    put.mockResolvedValue({ models: [] })
    const user = userEvent.setup()
    render(<FreeModelsCard />)
    expect(await screen.findByText('EnMaaS / glm-5-3')).toBeInTheDocument()
    await user.click(
      screen.getByRole('button', { name: 'Remove EnMaaS/glm-5-3 from free models' }),
    )
    await waitFor(() =>
      expect(put).toHaveBeenCalledWith('/api/admin/free-models', {
        models: [],
        expected_version: 'v1',
      }),
    )
    // A changed pair list invalidates the shared catalog cache.
    expect(resetCatalog).toHaveBeenCalled()
  })

  it('shows an empty state and disables Add until a pair is picked', async () => {
    get.mockResolvedValue({ models: [] })
    render(<FreeModelsCard />)
    expect(await screen.findByText('No free models declared.')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Add' })).toBeDisabled()
  })

  it('adopts the winning list on a 409 conflict', async () => {
    get.mockResolvedValue({ models: [], version: 'stale' })
    put.mockRejectedValueOnce(
      new ApiError(409, 'Conflict', {
        detail: {
          error: 'Free model list changed concurrently; reload and retry',
          models: [{ provider: 'google', model: 'gemini-2.5-pro' }],
          version: 'fresh',
        },
      }) as never,
    )
    const user = userEvent.setup()
    render(<FreeModelsCard />)
    await screen.findByText('No free models declared.')
    const providerTrigger = screen.getByRole('combobox', { name: 'Free model provider' })
    await user.click(providerTrigger)
    await user.click(await screen.findByText('Enmaas'))
    const modelInput = screen.getByPlaceholderText('Select model...')
    await user.click(modelInput)
    await user.click(await screen.findByText('glm-5-3-air'))
    await user.click(screen.getByRole('button', { name: 'Add' }))
    await waitFor(() =>
      expect(put).toHaveBeenCalledWith('/api/admin/free-models', {
        models: [{ provider: 'enmaas', model: 'glm-5-3-air' }],
        expected_version: 'stale',
      }),
    )
    // The losing client shows the winner's list and explains why.
    expect(await screen.findByText('google / gemini-2.5-pro')).toBeInTheDocument()
    expect(
      screen.getByText(/changed by someone else — showing their list/),
    ).toBeInTheDocument()
    expect(resetCatalog).toHaveBeenCalled()
  })

  it('keeps the admin picks when the save fails', async () => {
    get.mockResolvedValue({ models: [] })
    put.mockRejectedValueOnce(
      new Error('boom') as never,
    )
    const user = userEvent.setup()
    render(<FreeModelsCard />)
    await screen.findByText('No free models declared.')
    const providerTrigger = screen.getByRole('combobox', { name: 'Free model provider' })
    await user.click(providerTrigger)
    await user.click(await screen.findByText('Enmaas'))
    const modelInput = screen.getByPlaceholderText('Select model...')
    await user.click(modelInput)
    await user.click(await screen.findByText('glm-5-3-air'))
    await user.click(screen.getByRole('button', { name: 'Add' }))
    await waitFor(() =>
      expect(put).toHaveBeenCalledWith('/api/admin/free-models', {
        models: [{ provider: 'enmaas', model: 'glm-5-3-air' }],
        expected_version: undefined,
      }),
    )
    await waitFor(() =>
      expect(screen.getByRole('alert')).toBeInTheDocument(),
    )
    // Picks survive a failed save so the admin can retry.
    expect(screen.getByPlaceholderText('Select model...')).toBeInTheDocument()
    expect(resetCatalog).not.toHaveBeenCalled()
  })
})
