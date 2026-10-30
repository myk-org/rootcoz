/**
 * Guard: control styling comes from one place.
 *
 * `lib/controlStyles.ts` owns the shape of every single-line control. When a
 * component hard-codes a control shape instead, the product drifts — one page
 * ends up with pill inputs beside square buttons, which is exactly what this
 * test exists to prevent. It scans the sources rather than trusting review.
 */
import { describe, expect, it } from 'vitest'

/** Every source file as raw text, read at build time (no fs in the bundle). */
const SOURCES = import.meta.glob(['/src/**/*.{ts,tsx,js,jsx}', '!/src/**/__tests__/**'], {
  query: '?raw',
  import: 'default',
  eager: true,
}) as Record<string, string>

/** Files allowed to name a raw control shape (the source itself). */
const ALLOWED = new Set(['/src/lib/controlStyles.ts'])

const source = (path: string): string => SOURCES[path] ?? ''

describe('control styles come from one place', () => {
  it('no component hard-codes a rounded-full control', () => {
    const offenders = Object.entries(SOURCES)
      .filter(([file]) => !ALLOWED.has(file))
      .filter(([, text]) => {
        // A control is anything that is an input/select trigger; pills for
        // badges or avatars are not controls and are not matched here.
        return (
          /<Input\b[^>]*rounded-full/s.test(text) ||
          /<SelectTrigger\b[^>]*rounded-full/s.test(text) ||
          /rounded-full[^"'`]*border-border-default/s.test(text)
        )
      })
      .map(([file]) => file.replace(/^\/src\//, ''))

    expect(offenders, `hand-rolled pill controls in: ${offenders.join(', ')}`).toEqual([])
  })

  it('the primitives consume the shared class instead of re-declaring it', () => {
    expect(source('/src/components/ui/input.tsx')).toContain('CONTROL_CLASS')
    expect(source('/src/components/ui/select.tsx')).toContain('CONTROL_CLASS')
  })
})
