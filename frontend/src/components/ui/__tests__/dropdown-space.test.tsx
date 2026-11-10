/**
 * Guard: an open menu fits the screen.
 *
 * jsdom measures no layout, so this test cannot prove that a list near the
 * bottom edge flips instead of running off the page — the browser check does
 * that (see the PR). What it does prove is the wiring that makes it possible:
 * both primitives ask Radix to avoid the viewport edge and cap their height on
 * Radix's own measured space, never on a fixed pixel value that ignores where
 * the trigger sits.
 */
import { describe, expect, it } from 'vitest'

const SOURCES = import.meta.glob('/src/components/ui/*.tsx', {
  query: '?raw',
  import: 'default',
  eager: true,
}) as Record<string, string>

const source = (name: string) => SOURCES[`/src/components/ui/${name}.tsx`] ?? ''

/** Every list that sits inside a popover under its own filter row. */
const CONSUMERS = import.meta.glob(
  [
    '/src/components/shared/ServerProvidersCard.tsx',
    '/src/components/ui/searchable-multi-select.tsx',
    '/src/pages/report/BugCreationDialog.tsx',
  ],
  { query: '?raw', import: 'default', eager: true },
) as Record<string, string>

/** Each primitive, its collision props, and the height var Radix publishes on it. */
const PRIMITIVES = [
  { file: 'popover', heightVar: '--radix-popover-content-available-height' },
  { file: 'dropdown-menu', heightVar: '--radix-dropdown-menu-content-available-height' },
] as const

describe('menu primitives fit the space available', () => {
  it.each(PRIMITIVES)('$file asks Radix to avoid the viewport edge', ({ file }) => {
    const text = source(file)
    expect(text).toContain('avoidCollisions')
    expect(text).toContain('collisionPadding={8}')
  })

  it.each(PRIMITIVES)('$file caps its height on the measured space', ({ file, heightVar }) => {
    const text = source(file)
    expect(text).toContain(`var(${heightVar})`)
    // A fixed cap is the bug: it ignores how much room is actually left.
    expect(text).not.toMatch(/max-h-(64|96|48)\b/)
  })
})

describe('a list inside a popover leaves room for its own filter row', () => {
  it.each(Object.keys(CONSUMERS))('%s reserves the filter row instead of a fixed cap', (file) => {
    const text = CONSUMERS[file]
    expect(text).toContain('var(--radix-popover-content-available-height)')
    // A fixed cap lets the outer popover scroll the filter field out of view.
    expect(text).not.toMatch(/max-h-(64|96|48)\b/)
  })
})
