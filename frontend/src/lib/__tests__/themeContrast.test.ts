/**
 * Guard: dark- and light-theme WCAG 2.1 text & signal contrast (issues #587, #653).
 *
 * `tertiary` is the most-used meta text token (timestamps, hints, captions,
 * table meta, sort carets). In dark mode (#587) and later in light mode (#653),
 * low-contrast overrides slipped below WCAG AA (4.5:1) — and tinted signal
 * badges (`bg-signal-<color>/15`, `bg-glow-<color>`) can also drop under AA in light mode.
 * The fixes live in the token values in `theme.css`, so this test parses the
 * declared colours directly and fails the build if any guarded pair drops
 * below its contrast floor.
 */
import { describe, expect, it } from 'vitest'

const SOURCES = import.meta.glob('/src/*.css', {
  query: '?raw',
  import: 'default',
  eager: true,
}) as Record<string, string>

const themeCss = SOURCES['/src/theme.css'] ?? ''

const WCAG_AA_NORMAL_TEXT = 4.5

/** Text tokens under guard and the minimum contrast against any dark surface. */
const DARK_TEXT_FLOORS: Record<string, number> = {
  '--color-text-primary': 15,
  '--color-text-secondary': 12,
  '--color-text-tertiary': 9,
  '--color-text-link': WCAG_AA_NORMAL_TEXT,
}

/** Text tokens under guard and the minimum contrast against any light surface. */
const LIGHT_TEXT_FLOORS: Record<string, number> = {
  '--color-text-primary': WCAG_AA_NORMAL_TEXT,
  '--color-text-secondary': WCAG_AA_NORMAL_TEXT,
  '--color-text-tertiary': WCAG_AA_NORMAL_TEXT,
  '--color-text-link': WCAG_AA_NORMAL_TEXT,
}

/** Signal tokens used for text, badges, and status pills in both themes. */
const SIGNAL_TOKENS = [
  '--color-signal-red',
  '--color-signal-green',
  '--color-signal-orange',
  '--color-signal-amber',
  '--color-signal-blue',
  '--color-signal-purple',
]

/** Signal tokens that have matching `--color-glow-<color>` background tokens. */
const SIGNAL_GLOW_PAIRS: ReadonlyArray<readonly [string, string]> = [
  ['--color-signal-red', '--color-glow-red'],
  ['--color-signal-green', '--color-glow-green'],
  ['--color-signal-orange', '--color-glow-orange'],
  ['--color-signal-blue', '--color-glow-blue'],
  ['--color-signal-purple', '--color-glow-purple'],
]

/** The four surfaces a text token can be drawn on. */
const SURFACES = [
  '--color-surface-page',
  '--color-surface-card',
  '--color-surface-elevated',
  '--color-surface-hover',
]

/** Primary container surfaces where tinted badges (`bg-signal-<color>/15`, `bg-glow-<color>`) sit. */
const CONTAINER_SURFACES = [
  '--color-surface-page',
  '--color-surface-card',
  '--color-surface-elevated',
]

type Rgb = readonly [number, number, number]
type Rgba = readonly [number, number, number, number]

/** Raw `--color-<name>` declarations inside the block opened by `selector` (blocks here never nest). */
function rawDeclarations(selector: string): Record<string, string> {
  const start = themeCss.indexOf(selector)
  if (start < 0) throw new Error(`${selector} is missing from theme.css`)
  const open = themeCss.indexOf('{', start)
  const close = themeCss.indexOf('\n}', open)
  const body = themeCss.slice(open, close)

  return Object.fromEntries(
    [...body.matchAll(/(--color-[a-z-]+):\s*([^;]+);/g)].map(([, name, value]) => [
      name,
      value.trim().toLowerCase(),
    ]),
  )
}

const dark = rawDeclarations('@theme')
const light = rawDeclarations("html[data-theme='light']")

function parseHex(hex: string): Rgb {
  if (!/^#[0-9a-f]{6}$/.test(hex)) {
    throw new Error(`Expected 6-digit hex colour, got ${hex}`)
  }
  return [
    Number.parseInt(hex.slice(1, 3), 16),
    Number.parseInt(hex.slice(3, 5), 16),
    Number.parseInt(hex.slice(5, 7), 16),
  ]
}

function parseRgba(value: string): Rgba {
  const m = value.match(/^rgba\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*([\d.]+)\s*\)$/)
  if (!m) throw new Error(`Expected rgba() colour, got ${value}`)
  return [Number(m[1]), Number(m[2]), Number(m[3]), Number(m[4])]
}

function composite(fg: Rgba, bg: Rgb): Rgb {
  const [, , , a] = fg
  return [
    fg[0] * a + bg[0] * (1 - a),
    fg[1] * a + bg[1] * (1 - a),
    fg[2] * a + bg[2] * (1 - a),
  ]
}

function channel(value: number): number {
  const c = value / 255
  return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4
}

/** WCAG 2.1 relative luminance. */
function luminance([r, g, b]: Rgb): number {
  return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)
}

function contrastRgb(a: Rgb, b: Rgb): number {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x)
  return (hi + 0.05) / (lo + 0.05)
}

function contrast(a: string, b: string): number {
  return contrastRgb(parseHex(a), parseHex(b))
}

function assertSurfaceContrast(
  tokens: Record<string, string>,
  token: string,
  floor: number,
  surfaces: readonly string[],
) {
  const text = tokens[token]
  expect(text, `${token} is not declared`).toMatch(/^#[0-9a-f]{6}$/)

  const failures = surfaces.flatMap((surface) => {
    const bg = tokens[surface]
    expect(bg, `${surface} is not declared`).toMatch(/^#[0-9a-f]{6}$/)
    const ratio = contrast(text, bg)
    return ratio < floor
      ? [`${token} ${text} on ${surface} ${bg}: ${ratio.toFixed(2)}:1 < ${floor}:1`]
      : []
  })

  expect(failures, failures.join('\n')).toEqual([])
}

describe('dark theme text & signal contrast', () => {
  it.each(Object.entries(DARK_TEXT_FLOORS))('%s clears its floor on every dark surface', (token, floor) => {
    assertSurfaceContrast(dark, token, floor, SURFACES)
  })

  it.each(SIGNAL_TOKENS)('%s clears WCAG AA (4.5:1) on every dark surface', (token) => {
    assertSurfaceContrast(dark, token, WCAG_AA_NORMAL_TEXT, SURFACES)
  })
})

describe('light theme text & signal contrast', () => {
  it.each(Object.entries(LIGHT_TEXT_FLOORS))('%s clears WCAG AA (4.5:1) on every light surface', (token, floor) => {
    assertSurfaceContrast(light, token, floor, SURFACES)
  })

  it.each(SIGNAL_TOKENS)('%s clears WCAG AA (4.5:1) on every light surface', (token) => {
    assertSurfaceContrast(light, token, WCAG_AA_NORMAL_TEXT, SURFACES)
  })

  it.each(SIGNAL_TOKENS)('%s clears WCAG AA (4.5:1) on its 15%% badge tint over container surfaces', (token) => {
    const fg = parseHex(light[token])
    const failures = CONTAINER_SURFACES.flatMap((surface) => {
      const bg = composite([fg[0], fg[1], fg[2], 0.15], parseHex(light[surface]))
      const ratio = contrastRgb(fg, bg)
      return ratio < WCAG_AA_NORMAL_TEXT
        ? [`${token} ${light[token]} on 15% tint over ${surface} ${light[surface]}: ${ratio.toFixed(2)}:1 < ${WCAG_AA_NORMAL_TEXT}:1`]
        : []
    })

    expect(failures, failures.join('\n')).toEqual([])
  })

  it.each(SIGNAL_GLOW_PAIRS)('%s clears WCAG AA (4.5:1) on %s over container surfaces', (signalToken, glowToken) => {
    const fg = parseHex(light[signalToken])
    const glow = parseRgba(light[glowToken])
    const failures = CONTAINER_SURFACES.flatMap((surface) => {
      const bg = composite(glow, parseHex(light[surface]))
      const ratio = contrastRgb(fg, bg)
      return ratio < WCAG_AA_NORMAL_TEXT
        ? [`${signalToken} ${light[signalToken]} on ${glowToken} over ${surface} ${light[surface]}: ${ratio.toFixed(2)}:1 < ${WCAG_AA_NORMAL_TEXT}:1`]
        : []
    })

    expect(failures, failures.join('\n')).toEqual([])
  })

  it.each(['--color-signal-blue', '--color-signal-red'])(
    'white text clears WCAG AA (4.5:1) on solid %s button background',
    (token) => {
      const ratio = contrast('#ffffff', light[token])
      expect(ratio, `#ffffff on ${token} ${light[token]}: ${ratio.toFixed(2)}:1`).toBeGreaterThanOrEqual(
        WCAG_AA_NORMAL_TEXT,
      )
    },
  )
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
  it.each(Object.keys(DARK_TEXT_FLOORS))('%s is overridden in html[data-theme="light"]', (token) => {
    expect(light[token], `light theme has no ${token} override`).toMatch(/^#[0-9a-f]{6}$/)
  })

  it.each(Object.keys(DARK_TEXT_FLOORS))('%s differs between dark and light', (token) => {
    expect(light[token]).not.toBe(dark[token])
  })
})
