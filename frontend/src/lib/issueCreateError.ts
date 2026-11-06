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
