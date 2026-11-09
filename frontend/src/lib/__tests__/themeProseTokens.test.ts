/**
 * Guard: markdown follows the active theme.
 *
 * `@tailwindcss/typography` bakes a light palette into `--tw-prose-*` at build
 * time, and this build has no `dark:` variant — the theme lives on
 * `html[data-theme]` (see ThemeToggle). So `prose-invert` in the light theme is
 * light-on-white, and the markdown body is unreadable. theme.css must point
 * every prose variable at a design token instead. This scans the stylesheet
 * rather than trusting review.
 */
import { describe, expect, it } from 'vitest'

const SOURCES = import.meta.glob('/src/*.css', {
  query: '?raw',
  import: 'default',
  eager: true,
}) as Record<string, string>

const themeCss = SOURCES['/src/theme.css'] ?? ''

/** The prose variable → design token mapping the chat bubble depends on. */
const PROSE_TOKENS: Record<string, string> = {
  '--tw-prose-body': '--color-text-primary',
  '--tw-prose-headings': '--color-text-primary',
  '--tw-prose-bold': '--color-text-primary',
  '--tw-prose-links': '--color-text-link',
  '--tw-prose-code': '--color-text-secondary',
  '--tw-prose-pre-code': '--color-text-secondary',
  '--tw-prose-pre-bg': '--color-surface-elevated',
  '--tw-prose-quotes': '--color-text-secondary',
  '--tw-prose-counters': '--color-text-secondary',
  '--tw-prose-bullets': '--color-text-tertiary',
  '--tw-prose-hr': '--color-border-default',
  '--tw-prose-quote-borders': '--color-border-default',
  '--tw-prose-th-borders': '--color-border-default',
  '--tw-prose-td-borders': '--color-border-muted',
}

/** Every `--tw-prose-*` declaration in theme.css, as written. */
const declared = Object.fromEntries(
  [...themeCss.matchAll(/(--tw-prose-[a-z-]+):\s*([^;]+);/g)].map(([, name, value]) => [
    name,
    value.trim(),
  ]),
)

describe('markdown prose tokens follow the theme', () => {
  it('maps the prose palette to design tokens', () => {
    expect(declared).toMatchObject(
      Object.fromEntries(Object.entries(PROSE_TOKENS).map(([name, token]) => [name, `var(${token})`])),
    )
  })

  it('declares no hard-coded color in the prose palette', () => {
    // A literal would be stuck on one theme, which is the bug this guards.
    const literal = Object.entries(declared)
      .filter(([, value]) => !/^var\(--color-[a-z-]+\)$/.test(value))
      .map(([name, value]) => `${name}: ${value}`)

    expect(literal, `theme-fixed prose colors: ${literal.join(', ')}`).toEqual([])
  })
})
