import { describe, it, expect, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { WhatsNewDialog } from '../WhatsNewDialog'
import changelog from '@/changelog.json'

const LS_KEY = 'jji_last_seen_changelog_version'

describe('WhatsNewDialog', () => {
  beforeEach(() => {
    localStorage.clear()
  })

  it('shows the dialog when no version has been seen', () => {
    render(<WhatsNewDialog />)
    expect(screen.getByText("What's New")).toBeInTheDocument()
  })

  it('lists all entries from the latest changelog version', () => {
    render(<WhatsNewDialog />)
    for (const entry of changelog[0].entries) {
      expect(screen.getByText(entry.title)).toBeInTheDocument()
    }
  })

  it('does not show the dialog when the latest version has already been seen', () => {
    localStorage.setItem(LS_KEY, changelog[0].version)
    render(<WhatsNewDialog />)
    expect(screen.queryByText("What's New")).not.toBeInTheDocument()
  })

  it('shows again for a new version even if a previous version was dismissed', () => {
    localStorage.setItem(LS_KEY, '0.0.1')
    render(<WhatsNewDialog />)
    expect(screen.getByText("What's New")).toBeInTheDocument()
  })

  it('dismisses without saving version when "Got it" is clicked without checkbox', async () => {
    const user = userEvent.setup()
    render(<WhatsNewDialog />)
    await user.click(screen.getByRole('button', { name: /got it/i }))
    expect(screen.queryByText("What's New")).not.toBeInTheDocument()
    expect(localStorage.getItem(LS_KEY)).toBeNull()
  })

  it('saves the version to localStorage when "Don\'t show again" is checked before dismissing', async () => {
    const user = userEvent.setup()
    render(<WhatsNewDialog />)
    await user.click(screen.getByRole('checkbox'))
    await user.click(screen.getByRole('button', { name: /got it/i }))
    expect(localStorage.getItem(LS_KEY)).toBe(changelog[0].version)
  })
})
