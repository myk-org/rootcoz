import { describe, it, expect } from 'vitest'
import { defaultedNotice, unmatchedFailure } from '@/lib/pushResult'

describe('defaultedNotice', () => {
  it('describes TO INVESTIGATE defaulting as information, not a failure', () => {
    const { message, detail } = defaultedNotice(['test-a (classification: none)'])
    expect(message).toBe(
      '1 test(s) were pushed as TO INVESTIGATE because they have no classification',
    )
    expect(detail).toBe('test-a (classification: none)')
  })

  it('lists at most three entries and counts the rest', () => {
    const { detail } = defaultedNotice(['a', 'b', 'c', 'd', 'e'])
    expect(detail).toBe('a, b, c and 2 more')
  })
})

describe('unmatchedFailure', () => {
  it('keeps the honest pushed/skipped split for genuinely unpushed tests', () => {
    const { message, detail } = unmatchedFailure(3, ['test-z (classification: UNKNOWN)'])
    expect(message).toContain('Pushed 3 · skipped 1')
    expect(detail).toBe('test-z (classification: UNKNOWN)')
  })

  it('shows the split when pushed and skipped counts are equal', () => {
    const { message } = unmatchedFailure(2, [
      'test-a (classification: UNKNOWN)',
      'test-b (classification: UNKNOWN)',
    ])
    expect(message).toContain('Pushed 2 · skipped 2')
  })

  it('states only the reason when nothing was pushed', () => {
    const { message } = unmatchedFailure(0, [
      'test-a (classification: UNKNOWN)',
      'test-b (classification: UNKNOWN)',
    ])
    expect(message).not.toContain('Pushed 0')
    expect(message).toContain(
      '2 test(s) have no classification to map to a Report Portal defect type',
    )
  })
})
