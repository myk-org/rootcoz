import { render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { describe, expect, it, vi } from 'vitest'
import { AdminTranscriptsPage } from '@/pages/AdminTranscriptsPage'

const apiGet = vi.fn()
const apiGetText = vi.fn()
vi.mock('@/lib/api', () => ({
  api: {
    get: (...args: unknown[]) => apiGet(...args),
    getText: (...args: unknown[]) => apiGetText(...args),
  },
}))

const SESSION = '00000000-0000-0000-0000-000000000001'

function renderPage() {
  return render(
    <MemoryRouter>
      <AdminTranscriptsPage />
    </MemoryRouter>,
  )
}

describe('AdminTranscriptsPage', () => {
  it('lists transcripts with context and turns', async () => {
    apiGet.mockResolvedValue({
      transcripts_dir: '/data/pi-sidecar/transcripts',
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
      transcripts_dir: '/x',
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
      transcripts_dir: '/x',
      recording_enabled: true,
      count: 0,
      items: [],
    })
    renderPage()
    await waitFor(() => expect(screen.getByText(/No transcripts/i)).toBeInTheDocument())
  })

  it('surfaces load errors', async () => {
    apiGet.mockRejectedValue(new Error('boom'))
    renderPage()
    await waitFor(() => expect(screen.getByText('boom')).toBeInTheDocument())
  })
})
