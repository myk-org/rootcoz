import { ApiError, extractApiDetail } from '@/lib/api'

const GENERIC_CREATE_ERROR = 'Failed to create issue'

/** The message an issue-create dialog shows for a failed create.
 *
 *  The server's own wording when it sent one; the generic copy otherwise.
 *  `ApiError.message` is the HTTP status text ("API error 502: Bad Gateway"),
 *  which is never shown to the user — a body without a string `detail` falls
 *  back to the generic copy instead. */
export function createIssueFailureMessage(err: unknown): string {
  return (
    extractApiDetail(err)
    ?? (err instanceof ApiError || !(err instanceof Error) ? GENERIC_CREATE_ERROR : err.message)
  )
}

/** Whether a failed create left the outcome unknown, so a retry may duplicate.
 *
 *  The server spells "nothing was filed" into every refusal it can prove — a
 *  GitHub or Jira outage answers `502` with "… was not filed. Try again in a few
 *  minutes." — so an amber "may already have been filed" under that detail
 *  would tell the user both to retry and not to. Anything else (a timeout, a lost
 *  answer, no server response at all) stays unknown. */
export function createOutcomeUnknown(err: unknown): boolean {
  const detail = extractApiDetail(err)
  if (detail && /\bnot filed\b/i.test(detail)) return false
  return !(err instanceof ApiError) || err.status >= 500
}
