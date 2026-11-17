/**
 * Guard: dark-theme text contrast (issue #587).
 *
 * `tertiary` is the most-used text token (timestamps, hints, captions, table
 * meta) and it sat at 3.1–3.9:1 on the dark surfaces — below the WCAG AA floor
 * — while `secondary` was barely above it. The fix lives in the token values,
 * not in component typography, so this reads theme.css and measures the
 * declared colours instead of trusting review.
 *
 * The light theme is not part of this change: it must keep its own values for
 * all three tokens, or a "dark only" edit would silently hand light the dark
 * palette.
 */
import { describe, expect, it } from 'vitest'

const SOURCES = import.meta.glob('/src/*.css', {
  query: '?raw',
  import: 'default',
  eager: true,
}) as Record<string, string>

const themeCss = SOURCES['/src/theme.css'] ?? ''

/** Text tokens under guard and the minimum contrast against any dark surface. */
const FLOORS: Record<string, number> = {
  '--color-text-primary': 15,
  '--color-text-secondary': 12,
  '--color-text-tertiary': 9,
}

/** The four dark surfaces a text token can be drawn on. */
const SURFACES = [
  '--color-surface-page',
  '--color-surface-card',
  '--color-surface-elevated',
  '--color-surface-hover',
]

/** Declarations of the block opened by `selector` (blocks here never nest). */
function declarations(selector: string): Record<string, string> {
  const start = themeCss.indexOf(selector)
  if (start < 0) throw new Error(`${selector} is missing from theme.css`)
  const open = themeCss.indexOf('{', start)
  const close = themeCss.indexOf('\n}', open)
  const body = themeCss.slice(open, close)

  return Object.fromEntries(
    [...body.matchAll(/(--color-[a-z-]+):\s*(#[0-9a-fA-F]{3,8});/g)].map(([, name, value]) => [
      name,
      value.toLowerCase(),
    ]),
  )
}

const dark = declarations('@theme')
const light = declarations("html[data-theme='light']")

function channel(value: number): number {
  const c = value / 255
  return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4
}

/** WCAG 2.1 relative luminance. */
function luminance(hex: string): number {
  const [r, g, b] = [1, 3, 5].map((i) => channel(Number.parseInt(hex.slice(i, i + 2), 16)))
  return 0.2126 * r + 0.7152 * g + 0.0722 * b
}

function contrast(a: string, b: string): number {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x)
  return (hi + 0.05) / (lo + 0.05)
}

describe('dark theme text contrast', () => {
  it.each(Object.entries(FLOORS))('%s clears its floor on every dark surface', (token, floor) => {
    const text = dark[token]
    expect(text, `${token} is not declared in the dark @theme block`).toBeDefined()

    const failures = SURFACES.flatMap((surface) => {
      const ratio = contrast(text, dark[surface])
      return ratio < floor
        ? [`${token} ${text} on ${surface} ${dark[surface]}: ${ratio.toFixed(2)}:1 < ${floor}:1`]
        : []
    })

    expect(failures, failures.join('\n')).toEqual([])
  })
})

/** Body of the rule opened by `selector` (rules here never nest). */
function ruleBody(selector: string): string {
  const start = themeCss.indexOf(`${selector} {`)
  if (start < 0) throw new Error(`${selector} is missing from theme.css`)
  return themeCss.slice(start, themeCss.indexOf('\n}', start))
}

/**
 * Guard: the scrollbar hover is a surface, not text. It used to borrow
 * `--color-text-tertiary`, so re-tinting text turned every hovered thumb
 * near-white. It must own a token of its own.
 */
describe('scrollbar thumb hover', () => {
  const hover = ruleBody('*::-webkit-scrollbar-thumb:hover')

  it('does not derive its background from a text token', () => {
    expect(hover.match(/--color-text-[a-z-]+/g) ?? []).toEqual([])
  })

  it('uses --color-scrollbar-thumb-hover', () => {
    expect(hover).toContain('var(--color-scrollbar-thumb-hover)')
  })

  it.each([
    ['dark', dark],
    ['light', light],
  ])('%s theme declares it', (_theme, tokens) => {
    expect(tokens['--color-scrollbar-thumb-hover']).toMatch(/^#[0-9a-f]{6}$/)
  })

  it('differs between dark and light', () => {
    expect(light['--color-scrollbar-thumb-hover']).not.toBe(dark['--color-scrollbar-thumb-hover'])
  })
})

describe('light theme keeps its own text tokens', () => {
  it.each(Object.keys(FLOORS))('%s is overridden in html[data-theme="light"]', (token) => {
    expect(light[token], `light theme has no ${token} override`).toMatch(/^#[0-9a-f]{6}$/)
  })

  it.each(Object.keys(FLOORS))('%s differs between dark and light', (token) => {
    expect(light[token]).not.toBe(dark[token])
  })
})
