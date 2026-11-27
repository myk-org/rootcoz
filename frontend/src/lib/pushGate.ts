import { ApiError, extractApiDetail } from '@/lib/api'

/** Exporters whose manual pushes are gated on reviewed failures. */
export type GatedExporter = 'reportportal' | 'datadog' | 'greenwave'

/** Server Setting escape hatch per exporter, as named in user-facing text.
 *  `null` = no escape hatch: Greenwave's pushes always require a review. */
export const PUSH_GATE_SETTINGS: Record<GatedExporter, string | null> = {
  reportportal: 'RP_ALLOW_UNREVIEWED_PUSH',
  datadog: 'DD_ALLOW_UNREVIEWED_PUSH',
  greenwave: null,
}

/** Tooltip shown while a push button is disabled by the review gate. */
export function unreviewedPushTooltip(count: number, exporter: GatedExporter): string {
  const setting = PUSH_GATE_SETTINGS[exporter]
  const base = `${count} failure(s) not yet reviewed — review them before pushing`
  return setting ? `${base} (or ask an admin to enable ${setting})` : base
}

/** True when the server rejected a push with the unreviewed-failure gate (409). */
export function isUnreviewedPushRejection(err: unknown): boolean {
  return (
    err instanceof ApiError &&
    err.status === 409 &&
    !!extractApiDetail(err)?.includes('unreviewed failure(s)')
  )
}

/** Actionable message for a gate rejection — deliberately distinct from the
 *  generic red push-failure banner: points at the unreviewed failures on the
 *  page and names the setting an admin can flip to unblock. */
export function unreviewedPushRejectionMessage(err: unknown, exporter: GatedExporter): string {
  const detail = extractApiDetail(err) ?? 'job has unreviewed failure(s)'
  const setting = PUSH_GATE_SETTINGS[exporter]
  return setting
    ? `${detail} Review the unreviewed failures on this page and push again, or ask an admin to enable ${setting}.`
    : `${detail} Review the unreviewed failures on this page and push again.`
}

/** User-facing reason a push request was rejected, so operators see WHY
 *  (e.g. 403, invalid key) instead of a generic "push request failed". */
export function pushRejectionDetail(err: unknown): string {
  const detail = extractApiDetail(err)
  if (detail) return detail
  if (err instanceof Error && err.message) return err.message
  return 'push request failed'
}
