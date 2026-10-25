import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { beforeEach, describe, expect, it } from 'vitest'
import { ThemeToggle, THEME_STORAGE_KEY } from '../ThemeToggle'

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
})
