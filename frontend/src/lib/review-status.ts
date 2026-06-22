export const REVIEW_STATUS_OPTIONS = ['all', 'reviewed', 'not_reviewed'] as const
export type ReviewStatusFilter = typeof REVIEW_STATUS_OPTIONS[number]
export const REVIEW_STATUS_LABELS: Record<ReviewStatusFilter, string> = {
  all: 'All',
  reviewed: 'Reviewed',
  not_reviewed: 'Not reviewed',
}

/** Parse a URL search param into a ReviewStatusFilter value. */
export function parseReviewStatus(raw: string | null): ReviewStatusFilter {
  return raw === 'reviewed' || raw === 'not_reviewed' ? raw : 'all'
}
