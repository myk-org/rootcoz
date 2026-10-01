import { api } from '@/lib/api'
import { getUsername } from '@/lib/cookies'
import type { ReviewState, TrackedInEntry } from '@/types'

/** Every per-failure mutation is scoped by the reviewKey triple. */
export interface FailureScope {
  testName: string
  childJobName?: string
  childBuildNumber?: number
}

/** A selected failure group (one card) with all of its tests. */
export interface SelectedGroup {
  /** Failure-group id — the stable UUID-based key used by the card. */
  id: string
  testNames: string[]
  childJobName?: string
  childBuildNumber?: number
}

function scopeBody({ childJobName, childBuildNumber }: Omit<FailureScope, 'testName'>) {
  return { child_job_name: childJobName ?? '', child_build_number: childBuildNumber ?? 0 }
}

/* -- Per-failure endpoints (shared by the single-row controls and bulk bar) -- */

export function putReviewed(jobId: string, scope: FailureScope, reviewed: boolean) {
  return api.put<{ status: string; reviewed_by: string }>(`/results/${jobId}/reviewed`, {
    test_name: scope.testName,
    reviewed,
    ...scopeBody(scope),
  })
}

export function putOverrideClassification(jobId: string, scope: FailureScope, classification: string) {
  return api.put(`/results/${jobId}/override-classification`, {
    test_name: scope.testName,
    classification,
    ...scopeBody(scope),
  })
}

export function putOverridePattern(jobId: string, scope: FailureScope, pattern: string) {
  return api.put(`/results/${jobId}/override-pattern`, {
    test_name: scope.testName,
    pattern,
    ...scopeBody(scope),
  })
}

export function putTrackedIn(jobId: string, scope: FailureScope, url: string, type: string) {
  return api.put(`/results/${jobId}/tracked-in`, {
    test_name: scope.testName,
    url,
    type,
    ...scopeBody(scope),
  })
}

export function getTrackedIn(jobId: string) {
  return api.get<{ tracked_in: Record<string, TrackedInEntry[]> }>(`/results/${jobId}/tracked-in`)
}

/** Validate an issue URL before saving a tracked-in link.
 *  Empty input is not an error (callers handle "nothing typed"); returns the
 *  message to show, or null when the URL is usable as a link. */
export function trackedInUrlError(url: string): string | null {
  const trimmed = url.trim()
  if (!trimmed) return null
  try {
    const parsed = new URL(trimmed)
    if (!['http:', 'https:'].includes(parsed.protocol)) {
      return 'URL must start with http:// or https://'
    }
    return null
  } catch {
    return 'Please enter a valid URL (e.g., https://github.com/org/repo/issues/123)'
  }
}

/** Notify AllReviewedPrompt that review state changed. */
export function notifyReviewChanged(jobId: string) {
  setTimeout(() => window.dispatchEvent(new CustomEvent('rootcoz:review-changed', { detail: { jobId } })), 100)
}

/** Reviewer name for optimistic review state when the API omits it. */
function reviewActor(reviewedBy?: string) {
  return reviewedBy ?? getUsername()
}

/** Optimistic review state after a successful /reviewed call. */
export function scopedReviewState(reviewed: boolean, reviewedBy?: string): ReviewState {
  return { reviewed, username: reviewActor(reviewedBy), updated_at: new Date().toISOString() }
}

/** Flatten selected groups into one scope per affected test. */
export function selectedScopes(groups: SelectedGroup[]): FailureScope[] {
  return groups.flatMap((g) =>
    g.testNames.map((testName) => ({
      testName,
      childJobName: g.childJobName,
      childBuildNumber: g.childBuildNumber,
    })),
  )
}

/** Run a mutation over every scope in batches; never rejects — returns the failed scopes. */
export async function runBatched<T>(
  items: T[],
  fn: (item: T) => Promise<unknown>,
  batchSize = 5,
): Promise<T[]> {
  const failed: T[] = []
  for (let start = 0; start < items.length; start += batchSize) {
    const batch = items.slice(start, start + batchSize)
    const results = await Promise.allSettled(batch.map(fn))
    results.forEach((r, i) => {
      if (r.status === 'rejected') failed.push(batch[i])
    })
  }
  return failed
}
