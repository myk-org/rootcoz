import { describe, expect, it } from 'vitest'
import { formatCompactNumber, formatCost, unescapeCodeContent } from '../format'

describe('formatCompactNumber', () => {
  it('returns plain number below 1000', () => {
    expect(formatCompactNumber(0)).toBe('0')
    expect(formatCompactNumber(999)).toBe('999')
  })

  it('formats thousands as K', () => {
    expect(formatCompactNumber(1_000)).toBe('1K')
    expect(formatCompactNumber(1_500)).toBe('1.5K')
    expect(formatCompactNumber(15_000)).toBe('15K')
  })

  it('formats millions as M', () => {
    expect(formatCompactNumber(1_000_000)).toBe('1M')
    expect(formatCompactNumber(1_500_000)).toBe('1.5M')
    expect(formatCompactNumber(10_000_000)).toBe('10M')
  })
})

describe('unescapeCodeContent', () => {
  it('replaces literal \\n with actual newlines', () => {
    expect(unescapeCodeContent('line1\\nline2\\nline3')).toBe('line1\nline2\nline3')
  })

  it('replaces literal \\t with actual tabs', () => {
    expect(unescapeCodeContent('col1\\tcol2')).toBe('col1\tcol2')
  })

  it('handles mixed \\n and \\t', () => {
    expect(unescapeCodeContent('if x:\\n\\treturn y')).toBe('if x:\n\treturn y')
  })

  it('returns unchanged text when no escape sequences present', () => {
    const text = 'normal text with\nreal newlines'
    expect(unescapeCodeContent(text)).toBe(text)
  })

  it('handles empty string', () => {
    expect(unescapeCodeContent('')).toBe('')
  })

  it('preserves double-escaped \\\\n as literal backslash-n', () => {
    expect(unescapeCodeContent('\\\\n')).toBe('\\n')
  })

  it('preserves double-escaped \\\\t as literal backslash-t', () => {
    expect(unescapeCodeContent('\\\\t')).toBe('\\t')
  })

  it('handles mixed single and double-escaped sequences', () => {
    expect(unescapeCodeContent('line1\\nline2\\\\nstill-line2')).toBe('line1\nline2\\nstill-line2')
  })
})

describe('formatCost', () => {
  it('returns $0.00 for null/undefined/zero', () => {
    expect(formatCost(null)).toBe('$0.00')
    expect(formatCost(undefined)).toBe('$0.00')
    expect(formatCost(0)).toBe('$0.00')
  })

  it('uses 4 decimal places for values under $0.01', () => {
    expect(formatCost(0.005)).toBe('$0.0050')
    expect(formatCost(0.0012)).toBe('$0.0012')
  })

  it('uses 2 decimal places for values >= $0.01', () => {
    expect(formatCost(0.01)).toBe('$0.01')
    expect(formatCost(1.5)).toBe('$1.50')
    expect(formatCost(123.456)).toBe('$123.46')
  })
})
