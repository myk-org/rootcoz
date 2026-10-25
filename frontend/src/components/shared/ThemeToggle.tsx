import { useState } from 'react'
import { Moon, Sun } from 'lucide-react'

export const THEME_STORAGE_KEY = 'rootcoz-theme'

/**
 * Light/dark theme toggle. The active theme lives on
 * `<html data-theme>`; theme.css overrides the token values for
 * `data-theme="light"`, so every token consumer switches at once.
 * The initial value is applied pre-paint by the inline script in
 * index.html — the component only reads what is already there.
 */
export function ThemeToggle() {
  const [theme, setTheme] = useState<'dark' | 'light'>(() =>
    document.documentElement.dataset.theme === 'light' ? 'light' : 'dark',
  )

  const toggle = () => {
    const next = theme === 'dark' ? 'light' : 'dark'
    document.documentElement.dataset.theme = next
    try {
      localStorage.setItem(THEME_STORAGE_KEY, next)
    } catch {
      // private browsing — the toggle still works for this session
    }
    setTheme(next)
  }

  return (
    <button
      type="button"
      onClick={toggle}
      data-testid="theme-toggle"
      aria-label={`Switch to ${theme === 'dark' ? 'light' : 'dark'} theme`}
      className="flex h-8 items-center gap-1.5 rounded-full border border-border-default bg-surface-elevated px-3 text-xs font-medium text-text-secondary transition-colors duration-150 hover:bg-surface-hover hover:text-text-primary"
    >
      {theme === 'dark' ? <Sun className="h-3.5 w-3.5" /> : <Moon className="h-3.5 w-3.5" />}
      {theme === 'dark' ? 'Light' : 'Dark'}
    </button>
  )
}
