import { render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { describe, expect, it, vi } from 'vitest'
import { AdminTranscriptsPage } from '@/pages/AdminTranscriptsPage'
import { ApiError } from '@/lib/api'

const apiGet = vi.fn()
const apiGetText = vi.fn()
vi.mock('@/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api')>()
  return {
    ...actual,
    api: {
      get: (...args: unknown[]) => apiGet(...args),
      getText: (...args: unknown[]) => apiGetText(...args),
    },
  }
})

const SESSION = '00000000-0000-0000-0000-000000000001'

function renderPage(entry = '/admin/transcripts') {
  return render(
    <MemoryRouter initialEntries={[entry]}>
      <AdminTranscriptsPage />
    </MemoryRouter>,
  )
}

const listed = (items: unknown[]) =>
  Promise.resolve({ recording_enabled: true, count: items.length, items })

describe('AdminTranscriptsPage', () => {
  it('lists transcripts with context and turns', async () => {
    apiGet.mockResolvedValue({
      recording_enabled: true,
      count: 1,
      items: [
        {
          session_id: SESSION,
          size_bytes: 2048,
          modified_at: 1700000000,
          turns: 7,
          context: { job_id: 'job-1', call_type: 'primary', ai_model: 'opus-4-6' },
        },
      ],
    })
    renderPage()
    await waitFor(() => expect(screen.getByText(/primary/)).toBeInTheDocument())
    expect(screen.getByText('7')).toBeInTheDocument()
    expect(screen.getByText('2.0 KiB')).toBeInTheDocument()
  })

  it('shows the disabled-recording notice', async () => {
    apiGet.mockResolvedValue({
      recording_enabled: false,
      count: 0,
      items: [],
    })
    renderPage()
    await waitFor(() =>
      expect(screen.getByText(/recording is currently disabled/i)).toBeInTheDocument(),
    )
  })

  it('shows an empty state when no transcripts exist', async () => {
    apiGet.mockResolvedValue({
      recording_enabled: true,
      count: 0,
      items: [],
    })
    renderPage()
    await waitFor(() => expect(screen.getByText(/No transcripts/i)).toBeInTheDocument())
  })

  it('opens the transcript named by ?session= so an audit link lands on it', async () => {
    apiGet.mockResolvedValue({
      recording_enabled: true,
      count: 1,
      items: [{ session_id: SESSION, size_bytes: 10, modified_at: 1700000000, turns: 1, context: {} }],
    })
    apiGetText.mockResolvedValue('{"type":"turn"}\n')
    renderPage(`/admin/transcripts?session=${SESSION}`)

    await waitFor(() => expect(apiGetText).toHaveBeenCalledWith(`/api/admin/transcripts/${SESSION}`))
    expect(await screen.findByText('{"type":"turn"}')).toBeInTheDocument()
  })

  it('says a pruned ?session= transcript is not found instead of showing an HTTP status', async () => {
    apiGet.mockResolvedValue(listed([]) as never)
    apiGetText.mockRejectedValue(new ApiError(404, 'Not Found', { detail: 'Transcript not found' }))
    renderPage(`/admin/transcripts?session=${SESSION}`)

    await waitFor(() => expect(apiGetText).toHaveBeenCalledWith(`/api/admin/transcripts/${SESSION}`))
    expect(await screen.findByText(/^not found/)).toBeInTheDocument()
    expect(screen.getByText(/retention/)).toBeInTheDocument()
    expect(screen.queryByText(/API error 404/)).not.toBeInTheDocument()
  })

  it('surfaces load errors', async () => {
    apiGet.mockRejectedValue(new Error('boom'))
    renderPage()
    await waitFor(() => expect(screen.getByText('boom')).toBeInTheDocument())
  })
})
