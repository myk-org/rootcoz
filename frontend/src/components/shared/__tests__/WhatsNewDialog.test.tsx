import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { WhatsNewDialog } from '../WhatsNewDialog'

const LS_KEY = 'rootcoz_last_seen_changelog_version'

const mockRelease = {
  version: '4.0.1',
  name: 'v4.0.1',
  body: '### Bug Fixes\n- **SQLite concurrent access** — Enable WAL mode (#12)\n- **Dark theme scrollbars** — Add global scrollbar CSS (#13)',
  published_at: '2026-05-04T19:57:51Z',
  html_url: 'https://github.com/myk-org/rootcoz/releases/tag/v4.0.1',
}

vi.mock('@/lib/api', () => ({
  api: {
    get: vi.fn(),
  },
}))

describe('WhatsNewDialog', () => {
  beforeEach(async () => {
    localStorage.clear()
    vi.clearAllMocks()
    const { api } = vi.mocked(await import('@/lib/api'))
    api.get.mockResolvedValue(mockRelease)
  })

  it('shows the dialog when version has not been seen', async () => {
    render(<WhatsNewDialog />)
    await waitFor(() => {
      expect(screen.getByText("What's New")).toBeInTheDocument()
    })
  })

  it('parses and displays release entries', async () => {
    render(<WhatsNewDialog />)
    await waitFor(() => {
      expect(screen.getByText('SQLite concurrent access')).toBeInTheDocument()
      expect(screen.getByText('Dark theme scrollbars')).toBeInTheDocument()
    })
  })

  it('does not show when version was already seen', async () => {
    localStorage.setItem(LS_KEY, '4.0.1')
    render(<WhatsNewDialog />)
    // Wait a tick for the async effect to run
    await new Promise(r => setTimeout(r, 50))
    expect(screen.queryByText("What's New")).not.toBeInTheDocument()
  })

  it('shows for a new version even if a previous version was dismissed', async () => {
    localStorage.setItem(LS_KEY, '3.2.0')
    render(<WhatsNewDialog />)
    await waitFor(() => {
      expect(screen.getByText("What's New")).toBeInTheDocument()
    })
  })

  it('saves version when "Don\'t show again" is checked', async () => {
    const user = userEvent.setup()
    render(<WhatsNewDialog />)
    await waitFor(() => {
      expect(screen.getByText("What's New")).toBeInTheDocument()
    })
    await user.click(screen.getByRole('checkbox'))
    await user.click(screen.getByRole('button', { name: /got it/i }))
    expect(localStorage.getItem(LS_KEY)).toBe('4.0.1')
  })

  it('does not save version when dismissed without checkbox', async () => {
    const user = userEvent.setup()
    render(<WhatsNewDialog />)
    await waitFor(() => {
      expect(screen.getByText("What's New")).toBeInTheDocument()
    })
    await user.click(screen.getByRole('button', { name: /got it/i }))
    expect(localStorage.getItem(LS_KEY)).toBeNull()
  })

  it('shows link to full release notes', async () => {
    render(<WhatsNewDialog />)
    await waitFor(() => {
      expect(screen.getByText('View full release notes →')).toHaveAttribute('href', mockRelease.html_url)
    })
  })
})
