import { describe, it, expect } from 'vitest'

/**
 * Tests for the analysis-status filter logic used in DashboardPage.
 *
 * The filter works client-side:
 *   - "all"          → show every job
 *   - "analyzed"     → show only jobs with status === 'completed'
 *   - "not_analyzed" → show only jobs with status !== 'completed'
 */

interface MinimalJob {
  job_id: string
  status: string
}

/** Mirrors the filter logic in DashboardPage's `filtered` useMemo. */
function filterByAnalysisStatus(
  jobs: MinimalJob[],
  analysisStatus: 'all' | 'analyzed' | 'not_analyzed',
): MinimalJob[] {
  return jobs.filter((j) => {
    if (analysisStatus === 'analyzed' && j.status !== 'completed') return false
    if (analysisStatus === 'not_analyzed' && j.status === 'completed') return false
    return true
  })
}

/** Mirrors how analysisStatus is parsed from a URL search param. */
function parseAnalysisStatus(raw: string | null): 'all' | 'analyzed' | 'not_analyzed' {
  return raw === 'analyzed' || raw === 'not_analyzed' ? raw : 'all'
}

const JOBS: MinimalJob[] = [
  { job_id: '1', status: 'completed' },
  { job_id: '2', status: 'running' },
  { job_id: '3', status: 'pending' },
  { job_id: '4', status: 'failed' },
  { job_id: '5', status: 'completed' },
  { job_id: '6', status: 'waiting' },
  { job_id: '7', status: 'aborted' },
]

describe('DashboardAnalysisFilter', () => {
  describe('filterByAnalysisStatus', () => {
    it('"all" returns every job', () => {
      const result = filterByAnalysisStatus(JOBS, 'all')
      expect(result).toHaveLength(JOBS.length)
      expect(result.map((j) => j.job_id)).toEqual(['1', '2', '3', '4', '5', '6', '7'])
    })

    it('"analyzed" returns only completed jobs', () => {
      const result = filterByAnalysisStatus(JOBS, 'analyzed')
      expect(result).toHaveLength(2)
      expect(result.every((j) => j.status === 'completed')).toBe(true)
      expect(result.map((j) => j.job_id)).toEqual(['1', '5'])
    })

    it('"not_analyzed" returns only non-completed jobs', () => {
      const result = filterByAnalysisStatus(JOBS, 'not_analyzed')
      expect(result).toHaveLength(5)
      expect(result.every((j) => j.status !== 'completed')).toBe(true)
      expect(result.map((j) => j.job_id)).toEqual(['2', '3', '4', '6', '7'])
    })

    it('"analyzed" returns empty when no jobs are completed', () => {
      const nonCompleted = JOBS.filter((j) => j.status !== 'completed')
      const result = filterByAnalysisStatus(nonCompleted, 'analyzed')
      expect(result).toHaveLength(0)
    })

    it('"not_analyzed" returns empty when all jobs are completed', () => {
      const allCompleted = [
        { job_id: 'a', status: 'completed' },
        { job_id: 'b', status: 'completed' },
      ]
      const result = filterByAnalysisStatus(allCompleted, 'not_analyzed')
      expect(result).toHaveLength(0)
    })

    it('handles empty job list', () => {
      expect(filterByAnalysisStatus([], 'all')).toHaveLength(0)
      expect(filterByAnalysisStatus([], 'analyzed')).toHaveLength(0)
      expect(filterByAnalysisStatus([], 'not_analyzed')).toHaveLength(0)
    })
  })

  describe('parseAnalysisStatus', () => {
    it('returns "all" for null (no param)', () => {
      expect(parseAnalysisStatus(null)).toBe('all')
    })

    it('returns "all" for empty string', () => {
      expect(parseAnalysisStatus('')).toBe('all')
    })

    it('returns "all" for unknown values', () => {
      expect(parseAnalysisStatus('bogus')).toBe('all')
      expect(parseAnalysisStatus('ANALYZED')).toBe('all')
      expect(parseAnalysisStatus('completed')).toBe('all')
    })

    it('returns "analyzed" for "analyzed"', () => {
      expect(parseAnalysisStatus('analyzed')).toBe('analyzed')
    })

    it('returns "not_analyzed" for "not_analyzed"', () => {
      expect(parseAnalysisStatus('not_analyzed')).toBe('not_analyzed')
    })
  })
})
