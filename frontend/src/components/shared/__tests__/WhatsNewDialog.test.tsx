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

  describe('aggregated release notes (#341)', () => {
    const LS_BASELINE_KEY = 'rootcoz_last_server_version'

    const body471 = '- **SQLite concurrent access** — Enable WAL mode (#12)'
    const body472 = '- **Dark theme scrollbars** — Add global scrollbar CSS (#13)'

    /** Server on 4.7.0, latest 4.7.2, with 4.7.1 + 4.7.2 aggregated */
    const nagAggregated = {
      version: '4.7.2',
      name: 'v4.7.2',
      body: body472,
      published_at: '2026-06-01T00:00:00Z',
      html_url: 'https://github.com/myk-org/rootcoz/releases/tag/v4.7.2',
      server_version: '4.7.0',
      update_available: true,
      running_latest: false,
      releases: [
        {
          version: '4.7.1',
          name: 'v4.7.1',
          body: body471,
          published_at: '2026-05-28T00:00:00Z',
          html_url: 'https://github.com/myk-org/rootcoz/releases/tag/v4.7.1',
        },
        {
          version: '4.7.2',
          name: 'v4.7.2',
          body: body472,
          published_at: '2026-06-01T00:00:00Z',
          html_url: 'https://github.com/myk-org/rootcoz/releases/tag/v4.7.2',
        },
      ],
    }

    /** Post-upgrade: browser saw 4.7.0, server now runs the latest 4.7.2 */
    const whatsNewAggregated = {
      ...nagAggregated,
      server_version: '4.7.2',
      update_available: false,
      running_latest: true,
    }

    it('update nag shows entries from every release since the server version, grouped oldest-first', async () => {
      mockApiGet.mockResolvedValue(nagAggregated)
      render(<WhatsNewDialog />)
      await waitFor(() => {
        expect(screen.getByText('Update available')).toBeInTheDocument()
      })
      // Both versions' entries, under their own version headings
      expect(screen.getByText('v4.7.1')).toBeInTheDocument()
      expect(screen.getByText('SQLite concurrent access')).toBeInTheDocument()
      expect(screen.getByText('v4.7.2')).toBeInTheDocument()
      expect(screen.getByText('Dark theme scrollbars')).toBeInTheDocument()
      // Oldest group first
      const groups = screen.getAllByText(/^v4\.7\.[12]$/)
      expect(groups[0]).toHaveTextContent('v4.7.1')
      expect(groups[1]).toHaveTextContent('v4.7.2')
      // Nag subtitle still names the latest + running version
      expect(screen.getByText(/version 4\.7\.2 — your server runs 4\.7\.0/i)).toBeInTheDocument()
    })

    it('What\'s New after an upgrade aggregates from the last seen server version', async () => {
      localStorage.setItem(LS_BASELINE_KEY, '4.7.0')
      mockApiGet.mockResolvedValue(whatsNewAggregated)
      render(<WhatsNewDialog />)
      await waitFor(() => {
        expect(screen.getByText("What's New")).toBeInTheDocument()
      })
      expect(screen.getByText('SQLite concurrent access')).toBeInTheDocument()
      expect(screen.getByText('Dark theme scrollbars')).toBeInTheDocument()
    })

    it('nag "Don\'t show again" writes the update key and the baseline', async () => {
      mockApiGet.mockResolvedValue(nagAggregated)
      const user = userEvent.setup()
      render(<WhatsNewDialog />)
      await waitFor(() => {
        expect(screen.getByText('Update available')).toBeInTheDocument()
      })
      await user.click(screen.getByRole('checkbox'))
      await user.click(screen.getByRole('button', { name: /got it/i }))
      expect(localStorage.getItem(LS_UPDATE_KEY)).toBe('4.7.2')
      expect(localStorage.getItem(LS_BASELINE_KEY)).toBe('4.7.0')
      expect(localStorage.getItem(LS_SEEN_KEY)).toBeNull()
    })

    it('plain dismissal does not advance the baseline past what the fetch used', async () => {
      localStorage.setItem(LS_BASELINE_KEY, '4.6.0')
      mockApiGet.mockResolvedValue(nagAggregated)
      const user = userEvent.setup()
      render(<WhatsNewDialog />)
      await waitFor(() => {
        expect(screen.getByText('Update available')).toBeInTheDocument()
      })
      await user.click(screen.getByRole('button', { name: /got it/i }))
      // Baseline keeps the since value used for the fetch — not server_version
      expect(localStorage.getItem(LS_BASELINE_KEY)).toBe('4.6.0')
      expect(localStorage.getItem(LS_UPDATE_KEY)).toBeNull()
      expect(localStorage.getItem(LS_SEEN_KEY)).toBeNull()
    })

    it('writes the baseline on payload processing even when the popup is suppressed', async () => {
      localStorage.setItem(LS_BASELINE_KEY, '4.6.0')
      localStorage.setItem(LS_SEEN_KEY, '4.7.2')
      mockApiGet.mockResolvedValue(whatsNewAggregated)
      render(<WhatsNewDialog />)
      await waitFor(() => {
        expect(mockApiGet).toHaveBeenCalledWith('/api/releases/latest?since=4.6.0')
      })
      // Suppressed popup still persisted the baseline-used value — a reload
      // must not lose it (finding 1)
      expect(localStorage.getItem(LS_BASELINE_KEY)).toBe('4.6.0')
    })

    it('fresh mount with no baseline fetches without ?since and writes server_version after processing', async () => {
      mockApiGet.mockResolvedValue(whatsNewAggregated)
      render(<WhatsNewDialog />)
      await waitFor(() => {
        expect(mockApiGet).toHaveBeenCalledWith('/api/releases/latest')
      })
      await waitFor(() => {
        expect(screen.getByText("What's New")).toBeInTheDocument()
      })
      expect(localStorage.getItem(LS_BASELINE_KEY)).toBe('4.7.2')
    })

    it('"Don\'t show again" advances the baseline to server_version and writes the seen key', async () => {
      localStorage.setItem(LS_BASELINE_KEY, '4.7.0')
      mockApiGet.mockResolvedValue(whatsNewAggregated)
      const user = userEvent.setup()
      render(<WhatsNewDialog />)
      await waitFor(() => {
        expect(screen.getByText("What's New")).toBeInTheDocument()
      })
      await user.click(screen.getByRole('checkbox'))
      await user.click(screen.getByRole('button', { name: /got it/i }))
      expect(localStorage.getItem(LS_BASELINE_KEY)).toBe('4.7.2')
      expect(localStorage.getItem(LS_SEEN_KEY)).toBe('4.7.2')
    })

    it('post-upgrade: plain dismiss keeps the baseline, re-show fetches the same since and identical groups', async () => {
      localStorage.setItem(LS_BASELINE_KEY, '4.7.0')
      mockApiGet.mockResolvedValue(whatsNewAggregated)
      const user = userEvent.setup()
      const { unmount } = render(<WhatsNewDialog />)
      await waitFor(() => {
        expect(screen.getByText('SQLite concurrent access')).toBeInTheDocument()
        expect(screen.getByText('Dark theme scrollbars')).toBeInTheDocument()
      })
      await user.click(screen.getByRole('button', { name: /got it/i }))
      // Plain dismissal keeps the old since (finding 4)
      expect(localStorage.getItem(LS_BASELINE_KEY)).toBe('4.7.0')
      unmount()

      mockApiGet.mockClear()
      render(<WhatsNewDialog />)
      await waitFor(() => {
        expect(mockApiGet).toHaveBeenCalledWith('/api/releases/latest?since=4.7.0')
      })
      // Re-shown dialog shows the SAME aggregated groups
      await waitFor(() => {
        expect(screen.getByText('v4.7.1')).toBeInTheDocument()
        expect(screen.getByText('SQLite concurrent access')).toBeInTheDocument()
        expect(screen.getByText('v4.7.2')).toBeInTheDocument()
        expect(screen.getByText('Dark theme scrollbars')).toBeInTheDocument()
      })
    })

    it('a null release body does not crash — other groups still render', async () => {
      mockApiGet.mockResolvedValue({
        ...whatsNewAggregated,
        releases: [
          { ...whatsNewAggregated.releases[0], body: null as unknown as string },
          whatsNewAggregated.releases[1],
        ],
      })
      render(<WhatsNewDialog />)
      await waitFor(() => {
        expect(screen.getByText("What's New")).toBeInTheDocument()
      })
      expect(screen.getByText('v4.7.1')).toBeInTheDocument()
      expect(screen.getByText('Dark theme scrollbars')).toBeInTheDocument()
    })

    it('a dismissed nag for the latest version does not re-show, even with releases', async () => {
      localStorage.setItem(LS_UPDATE_KEY, '4.7.2')
      mockApiGet.mockResolvedValue(nagAggregated)
      render(<WhatsNewDialog />)
      await waitFor(() => {
        expect(mockApiGet).toHaveBeenCalled()
      })
      await new Promise((r) => setTimeout(r, 50))
      expect(screen.queryByText('Update available')).not.toBeInTheDocument()
    })

    it('What\'s New seen version still suppresses the popup with releases present', async () => {
      localStorage.setItem(LS_SEEN_KEY, '4.7.2')
      mockApiGet.mockResolvedValue(whatsNewAggregated)
      render(<WhatsNewDialog />)
      await waitFor(() => {
        expect(mockApiGet).toHaveBeenCalled()
      })
      await new Promise((r) => setTimeout(r, 50))
      expect(screen.queryByText("What's New")).not.toBeInTheDocument()
    })

    it('empty releases array falls back to the single top-level release body', async () => {
      mockApiGet.mockResolvedValue({ ...nagAggregated, releases: [] })
      render(<WhatsNewDialog />)
      await waitFor(() => {
        expect(screen.getByText('Update available')).toBeInTheDocument()
      })
      // Single-release rendering: no version group headings, only the top-level body's entries
      expect(screen.getByText('Dark theme scrollbars')).toBeInTheDocument()
      expect(screen.queryByText('SQLite concurrent access')).not.toBeInTheDocument()
      expect(screen.queryByText('v4.7.1')).not.toBeInTheDocument()
      expect(screen.getByText('View full release notes →')).toHaveAttribute('href', nagAggregated.html_url)
    })

    it('fetches with ?since= when the baseline is stored, without it when absent', async () => {
      render(<WhatsNewDialog />)
      await waitFor(() => {
        expect(mockApiGet).toHaveBeenCalledWith('/api/releases/latest')
      })
      mockApiGet.mockClear()

      localStorage.setItem(LS_BASELINE_KEY, '4.7.0')
      const { unmount } = render(<WhatsNewDialog />)
      await waitFor(() => {
        expect(mockApiGet).toHaveBeenCalledWith('/api/releases/latest?since=4.7.0')
      })
      unmount()
    })

    it('What\'s New with no parsed entries across all groups does not open', async () => {
      mockApiGet.mockResolvedValue({
        ...whatsNewAggregated,
        body: 'Prose only, no bullets.',
        releases: [
          { ...whatsNewAggregated.releases[0], body: 'Prose only, no bullets.' },
          { ...whatsNewAggregated.releases[1], body: '' },
        ],
      })
      render(<WhatsNewDialog />)
      await waitFor(() => {
        expect(mockApiGet).toHaveBeenCalled()
      })
      await new Promise((r) => setTimeout(r, 50))
      expect(screen.queryByText("What's New")).not.toBeInTheDocument()
    })
  })
})
