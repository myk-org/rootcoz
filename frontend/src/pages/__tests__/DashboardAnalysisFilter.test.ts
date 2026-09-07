import { describe, it, expect } from 'vitest'
import { parseReviewStatus, type ReviewStatusFilter } from '@/lib/review-status'

/**
 * Tests for the review-status filter logic used in DashboardPage and ReportsPage.
 *
 * The filter works client-side using reviewed_count from the API:
 *   - "all"          → show every job
 *   - "reviewed"     → show only jobs where reviewed_count > 0
 *   - "not_reviewed" → show only jobs where reviewed_count === 0
 */

interface MinimalJob {
  job_id: string
  reviewed_count: number
}

/** Mirrors the filter logic in DashboardPage's `filtered` useMemo. */
function filterByReviewStatus(
  jobs: MinimalJob[],
  reviewStatus: ReviewStatusFilter,
): MinimalJob[] {
  return jobs.filter((j) => {
    if (reviewStatus === 'reviewed' && j.reviewed_count === 0) return false
    if (reviewStatus === 'not_reviewed' && j.reviewed_count > 0) return false
    return true
  })
}

const JOBS: MinimalJob[] = [
  { job_id: '1', reviewed_count: 3 },
  { job_id: '2', reviewed_count: 0 },
  { job_id: '3', reviewed_count: 0 },
  { job_id: '4', reviewed_count: 1 },
  { job_id: '5', reviewed_count: 5 },
  { job_id: '6', reviewed_count: 0 },
  { job_id: '7', reviewed_count: 0 },
]

describe('DashboardReviewStatusFilter', () => {
  describe('filterByReviewStatus', () => {
    it('"all" returns every job', () => {
      const result = filterByReviewStatus(JOBS, 'all')
      expect(result).toHaveLength(JOBS.length)
      expect(result.map((j) => j.job_id)).toEqual(['1', '2', '3', '4', '5', '6', '7'])
    })

    it('"reviewed" returns only jobs with reviewed_count > 0', () => {
      const result = filterByReviewStatus(JOBS, 'reviewed')
      expect(result).toHaveLength(3)
      expect(result.every((j) => j.reviewed_count > 0)).toBe(true)
      expect(result.map((j) => j.job_id)).toEqual(['1', '4', '5'])
    })

    it('"not_reviewed" returns only jobs with reviewed_count === 0', () => {
      const result = filterByReviewStatus(JOBS, 'not_reviewed')
      expect(result).toHaveLength(4)
      expect(result.every((j) => j.reviewed_count === 0)).toBe(true)
      expect(result.map((j) => j.job_id)).toEqual(['2', '3', '6', '7'])
    })

    it('"reviewed" returns empty when no jobs have reviews', () => {
      const noReviews = JOBS.filter((j) => j.reviewed_count === 0)
      const result = filterByReviewStatus(noReviews, 'reviewed')
      expect(result).toHaveLength(0)
    })

    it('"not_reviewed" returns empty when all jobs have reviews', () => {
      const allReviewed = [
        { job_id: 'a', reviewed_count: 2 },
        { job_id: 'b', reviewed_count: 1 },
      ]
      const result = filterByReviewStatus(allReviewed, 'not_reviewed')
      expect(result).toHaveLength(0)
    })

    it('handles empty job list', () => {
      expect(filterByReviewStatus([], 'all')).toHaveLength(0)
      expect(filterByReviewStatus([], 'reviewed')).toHaveLength(0)
      expect(filterByReviewStatus([], 'not_reviewed')).toHaveLength(0)
    })
  })

  describe('parseReviewStatus', () => {
    it('returns "all" for null (no param)', () => {
      expect(parseReviewStatus(null)).toBe('all')
    })

    it('returns "all" for empty string', () => {
      expect(parseReviewStatus('')).toBe('all')
    })

    it('returns "all" for unknown values', () => {
      expect(parseReviewStatus('bogus')).toBe('all')
      expect(parseReviewStatus('REVIEWED')).toBe('all')
      expect(parseReviewStatus('completed')).toBe('all')
      expect(parseReviewStatus('analyzed')).toBe('all')
      expect(parseReviewStatus('not_analyzed')).toBe('all')
    })

    it('returns "reviewed" for "reviewed"', () => {
      expect(parseReviewStatus('reviewed')).toBe('reviewed')
    })

    it('returns "not_reviewed" for "not_reviewed"', () => {
      expect(parseReviewStatus('not_reviewed')).toBe('not_reviewed')
    })
  })
})

describe('parseAnalysisState', () => {
  it('parses submitted and analyzed', async () => {
    const { parseAnalysisState } = await import('@/lib/analysis-state')
    expect(parseAnalysisState(null)).toBe('all')
    expect(parseAnalysisState('submitted')).toBe('submitted')
    expect(parseAnalysisState('analyzed')).toBe('analyzed')
    expect(parseAnalysisState('bogus')).toBe('all')
  })
})
