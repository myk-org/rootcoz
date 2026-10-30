import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { beforeEach, describe, expect, it } from 'vitest'
import { ThemeToggle, THEME_STORAGE_KEY } from '../ThemeToggle'
import { TEXT_BUTTON_CLASS } from '@/lib/controlStyles'

describe('ThemeToggle', () => {
  beforeEach(() => {
    localStorage.clear()
    delete document.documentElement.dataset.theme
    cleanup()
  })

  it('toggles from dark to light and persists the choice', () => {
    render(<ThemeToggle />)
    const button = screen.getByTestId('theme-toggle')
    expect(button.textContent).toContain('Light')
    fireEvent.click(button)
    expect(document.documentElement.dataset.theme).toBe('light')
    expect(localStorage.getItem(THEME_STORAGE_KEY)).toBe('light')
    expect(button.textContent).toContain('Dark')
  })

  it('toggles back to dark', () => {
    render(<ThemeToggle />)
    const button = screen.getByTestId('theme-toggle')
    fireEvent.click(button)
    fireEvent.click(button)
    expect(document.documentElement.dataset.theme).toBe('dark')
    expect(localStorage.getItem(THEME_STORAGE_KEY)).toBe('dark')
  })

  it('reflects an existing light theme on mount', () => {
    document.documentElement.dataset.theme = 'light'
    render(<ThemeToggle />)
    expect(screen.getByTestId('theme-toggle').textContent).toContain('Dark')
  })

  it('looks like the header text buttons (Guide/Feedback), not a bordered pill', () => {
    render(<ThemeToggle />)
    const button = screen.getByTestId('theme-toggle')
    // One shared look for header text buttons: no border, no pill, no own box.
    expect(button.className).toBe(TEXT_BUTTON_CLASS)
    expect(button.className).not.toMatch(/\bborder\b/)
    expect(button.className).not.toContain('rounded-full')
  })
})
