import { useState } from 'react'
import { Moon, Sun } from 'lucide-react'
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from '@/components/ui/tooltip'
import { TEXT_BUTTON_CLASS } from '@/lib/controlStyles'

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
    // Header text button, same look as Guide/Feedback: no box of its own.
    <TooltipProvider delayDuration={200}>
      <Tooltip>
        <TooltipTrigger asChild>
          <button
            type="button"
            onClick={toggle}
            data-testid="theme-toggle"
            aria-label={`Switch to ${theme === 'dark' ? 'light' : 'dark'} theme`}
            className={TEXT_BUTTON_CLASS}
          >
            {theme === 'dark' ? (
              <Sun className="h-4 w-4 shrink-0" />
            ) : (
              <Moon className="h-4 w-4 shrink-0" />
            )}
            {theme === 'dark' ? 'Light' : 'Dark'}
          </button>
        </TooltipTrigger>
        <TooltipContent>Switch to {theme === 'dark' ? 'light' : 'dark'} theme</TooltipContent>
      </Tooltip>
    </TooltipProvider>
  )
}
