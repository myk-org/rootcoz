import { Lock } from 'lucide-react'

/**
 * Explains that server credentials are unavailable to the current user.
 *
 * Rendered as a bordered, amber callout rather than a grey hint line: the
 * notice is the reason the picker cannot proceed, and a user skimming the
 * form used to miss it entirely because it matched the surrounding helper
 * text. The user key is a valid alternative, so this is a warning, not an
 * error.
 */
export function CredentialAccessNotice({ message, className = '' }: {
  /** Overrides the default wording (e.g. when a prior run used server credentials). */
  message?: string
  className?: string
}) {
  return (
    <div
      role="note"
      className={`flex items-start gap-2 rounded-md border border-signal-amber/30 bg-signal-amber/5 px-3 py-2 text-xs font-medium text-signal-amber ${className}`}
    >
      <Lock className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden="true" />
      <span>{message ?? 'Server credentials are restricted. Ask an admin for access, or use your own AI key.'}</span>
    </div>
  )
}
