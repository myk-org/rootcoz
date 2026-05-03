import { describe, it, expect } from 'vitest'
import { reviewKey } from '../ReportContext'

describe('reviewKey', () => {
  it('returns just testName when no child job', () => {
    expect(reviewKey('my.Test')).toBe('my.Test')
  })

  it('returns child format when child job provided', () => {
    expect(reviewKey('my.Test', 'child-job', 5)).toBe('child-job#5::my.Test')
  })

  it('returns just testName when childJobName is empty', () => {
    expect(reviewKey('my.Test', '', 0)).toBe('my.Test')
  })
})
