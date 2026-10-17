import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { WhatsNewDialog } from '../WhatsNewDialog'

const LS_SEEN_KEY = 'rootcoz_last_seen_changelog_version'
const LS_UPDATE_KEY = 'rootcoz_last_dismissed_update'

const mockBody = '### Bug Fixes\n- **SQLite concurrent access** — Enable WAL mode (#12)\n- **Dark theme scrollbars** — Add global scrollbar CSS (#13)'

/** Release payload for a server running the latest version */
const upToDateRelease = {
  version: '4.0.1',
  name: 'v4.0.1',
  body: mockBody,
  published_at: '2026-05-04T19:57:51Z',
  html_url: 'https://github.com/myk-org/rootcoz/releases/tag/v4.0.1',
  server_version: '4.0.1',
  update_available: false,
  running_latest: true,
}

/** Release payload for a server running an older version */
const updateAvailableRelease = {
  ...upToDateRelease,
  server_version: '3.9.0',
  update_available: true,
  running_latest: false,
}

vi.mock('@/lib/api', () => ({
  api: {
    get: vi.fn(),
  },
}))

import { api } from '@/lib/api'

const mockApiGet = api.get as unknown as ReturnType<typeof vi.fn>

describe('WhatsNewDialog', () => {
  beforeEach(() => {
    localStorage.clear()
    vi.clearAllMocks()
    mockApiGet.mockResolvedValue(upToDateRelease)
  })

  describe('What\'s New mode (server up to date)', () => {
    it('shows the dialog when the running version has not been seen', async () => {
      render(<WhatsNewDialog />)
      await waitFor(() => {
        expect(screen.getByText("What's New")).toBeInTheDocument()
      })
      expect(screen.queryByText('Update available')).not.toBeInTheDocument()
    })

    it('does not show when the version was already seen', async () => {
      localStorage.setItem(LS_SEEN_KEY, '4.0.1')
      render(<WhatsNewDialog />)
      await waitFor(() => {
        expect(screen.queryByText("What's New")).not.toBeInTheDocument()
      })
    })

    it('shows for a new version even if a previous version was dismissed', async () => {
      localStorage.setItem(LS_SEEN_KEY, '3.2.0')
      render(<WhatsNewDialog />)
      await waitFor(() => {
        expect(screen.getByText("What's New")).toBeInTheDocument()
      })
    })

    it('saves version to the changelog key when "Don\'t show again" is checked', async () => {
      const user = userEvent.setup()
      render(<WhatsNewDialog />)
      await waitFor(() => {
        expect(screen.getByText("What's New")).toBeInTheDocument()
      })
      await user.click(screen.getByRole('checkbox'))
      await user.click(screen.getByRole('button', { name: /got it/i }))
      expect(localStorage.getItem(LS_SEEN_KEY)).toBe('4.0.1')
      expect(localStorage.getItem(LS_UPDATE_KEY)).toBeNull()
    })

    it('does not save version when dismissed without checkbox', async () => {
      const user = userEvent.setup()
      render(<WhatsNewDialog />)
      await waitFor(() => {
        expect(screen.getByText("What's New")).toBeInTheDocument()
      })
      await user.click(screen.getByRole('button', { name: /got it/i }))
      expect(localStorage.getItem(LS_SEEN_KEY)).toBeNull()
    })

    it('does not show when the server runs a newer version than the latest release (dev builds)', async () => {
      mockApiGet.mockResolvedValue({
        ...upToDateRelease,
        server_version: '4.1.0-dev',
        running_latest: false,
      })
      render(<WhatsNewDialog />)
      await waitFor(() => {
        expect(mockApiGet).toHaveBeenCalledWith('/api/releases/latest')
      })
      // Give the effect a moment — nothing must render
      await new Promise((r) => setTimeout(r, 50))
      expect(screen.queryByText("What's New")).not.toBeInTheDocument()
      expect(screen.queryByText('Update available')).not.toBeInTheDocument()
    })

    it('shows What\'s New for equivalent version spellings (4.0.1 release, 4.0.1.0 server)', async () => {
      // The backend decides equivalence via running_latest — the dialog
      // must not string-compare versions
      mockApiGet.mockResolvedValue({
        ...upToDateRelease,
        server_version: '4.0.1.0',
      })
      render(<WhatsNewDialog />)
      await waitFor(() => {
        expect(screen.getByText("What's New")).toBeInTheDocument()
      })
    })
  })

  describe('Update available mode (server older than latest)', () => {
    it('shows the update nag mentioning the running version', async () => {
      mockApiGet.mockResolvedValue(updateAvailableRelease)
      render(<WhatsNewDialog />)
      await waitFor(() => {
        expect(screen.getByText('Update available')).toBeInTheDocument()
      })
      expect(screen.getByText(/your server runs 3\.9\.0/i)).toBeInTheDocument()
      expect(screen.queryByText("What's New")).not.toBeInTheDocument()
    })

    it('does not show the nag after it was dismissed', async () => {
      localStorage.setItem(LS_UPDATE_KEY, '4.0.1')
      mockApiGet.mockResolvedValue(updateAvailableRelease)
      render(<WhatsNewDialog />)
      await waitFor(() => {
        expect(mockApiGet).toHaveBeenCalledWith('/api/releases/latest')
      })
      await new Promise((r) => setTimeout(r, 50))
      expect(screen.queryByText('Update available')).not.toBeInTheDocument()
    })

    it('shows the nag even when the release notes have no bullet entries', async () => {
      mockApiGet.mockResolvedValue({
        ...updateAvailableRelease,
        body: 'Prose-only release notes with no bullets.',
      })
      render(<WhatsNewDialog />)
      await waitFor(() => {
        expect(screen.getByText('Update available')).toBeInTheDocument()
      })
    })

    it('saves version to the update key when "Don\'t show again" is checked', async () => {
      mockApiGet.mockResolvedValue(updateAvailableRelease)
      const user = userEvent.setup()
      render(<WhatsNewDialog />)
      await waitFor(() => {
        expect(screen.getByText('Update available')).toBeInTheDocument()
      })
      await user.click(screen.getByRole('checkbox'))
      await user.click(screen.getByRole('button', { name: /got it/i }))
      expect(localStorage.getItem(LS_UPDATE_KEY)).toBe('4.0.1')
      expect(localStorage.getItem(LS_SEEN_KEY)).toBeNull()
    })

    it('a dismissed update nag does not suppress the What\'s New popup after the server is updated', async () => {
      // User dismissed the 4.0.1 update nag while running 3.9.0...
      localStorage.setItem(LS_UPDATE_KEY, '4.0.1')
      // ...then the admin updated the server: 4.0.1 is now running
      mockApiGet.mockResolvedValue(upToDateRelease)
      render(<WhatsNewDialog />)
      await waitFor(() => {
        expect(screen.getByText("What's New")).toBeInTheDocument()
      })
    })
  })

  it('parses and displays release entries in both modes', async () => {
    mockApiGet.mockResolvedValue(updateAvailableRelease)
    render(<WhatsNewDialog />)
    await waitFor(() => {
      expect(screen.getByText('SQLite concurrent access')).toBeInTheDocument()
      expect(screen.getByText('Dark theme scrollbars')).toBeInTheDocument()
    })
  })

  it('shows link to full release notes', async () => {
    render(<WhatsNewDialog />)
    await waitFor(() => {
      expect(screen.getByText('View full release notes →')).toHaveAttribute('href', upToDateRelease.html_url)
    })
  })
})
