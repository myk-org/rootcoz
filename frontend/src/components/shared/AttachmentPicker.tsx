import { useId, useRef } from 'react'
import { Paperclip, X } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from '@/components/ui/tooltip'
import { describeAttachmentDelivery } from '@/lib/attachments'
import type { IssueAttachmentSummary } from '@/types'

interface AttachmentPickerProps {
  files: File[]
  onChange: (files: File[]) => void
  disabledReason?: string
  hint?: string
}

export function AttachmentPicker({ files, onChange, disabledReason, hint }: AttachmentPickerProps) {
  const input = useRef<HTMLInputElement>(null)
  const reasonId = useId()
  return (
    <div className="space-y-2 min-w-0">
      <input ref={input} type="file" multiple hidden aria-label="Issue attachments" disabled={!!disabledReason}
        onChange={event => {
          onChange([...files, ...Array.from(event.target.files ?? [])])
          event.target.value = ''
        }} />
      <TooltipProvider>
        <Tooltip>
          <TooltipTrigger asChild>
            <span tabIndex={disabledReason ? 0 : undefined} aria-describedby={disabledReason ? reasonId : undefined} className="inline-flex">
              <Button type="button" variant="outline" size="sm" disabled={!!disabledReason} onClick={() => input.current?.click()}>
                <Paperclip className="h-4 w-4" /> Attach files
              </Button>
            </span>
          </TooltipTrigger>
          {disabledReason && <TooltipContent>{disabledReason}</TooltipContent>}
        </Tooltip>
      </TooltipProvider>
      {disabledReason && <p id={reasonId} className="text-xs text-text-secondary">{disabledReason}</p>}
      {hint && <p className="text-xs text-text-tertiary">{hint}</p>}
      {files.length > 0 && <ul className="space-y-1 text-sm text-text-secondary">
        {files.map((file, index) => <li key={index} className="flex items-center gap-2 min-w-0">
          <span className="break-all min-w-0">{file.name}</span>
          <span className="shrink-0 text-xs text-text-tertiary">{file.size.toLocaleString()} bytes</span>
          <Button type="button" variant="ghost" size="sm" aria-label={`Remove ${file.name}`} disabled={!!disabledReason}
            onClick={() => onChange(files.filter((_, i) => i !== index))}><X className="h-4 w-4" /></Button>
        </li>)}
      </ul>}
    </div>
  )
}

export function IssueAttachmentDeliverySummary({ summary }: { summary: IssueAttachmentSummary }) {
  const hasFallbackOnly = summary.files.some((file) => file.delivery === 'fallback_only')
  const hasNativeUnknown = summary.files.some((file) => file.delivery === 'native_outcome_unknown')

  return (
    <div
      data-testid="attachment-delivery-summary"
      className="w-full max-w-xl rounded-md border border-border-muted bg-surface-elevated p-3 text-left space-y-2"
    >
      <div className="flex flex-wrap items-center justify-between gap-2 text-xs text-text-secondary">
        <span className="font-medium text-text-primary">
          Attachments ({summary.count})
        </span>
        {summary.types.length > 0 && (
          <span className="text-text-tertiary">{summary.types.join(', ')}</span>
        )}
      </div>
      <ul className="space-y-1 text-xs text-text-secondary">
        {summary.files.map((file) => (
          <li
            key={file.id}
            className="flex flex-col gap-0.5 sm:flex-row sm:items-baseline sm:justify-between sm:gap-3"
          >
            <span className="font-mono text-text-primary break-all">{file.filename}</span>
            <span className="text-text-tertiary shrink-0">{describeAttachmentDelivery(file)}</span>
          </li>
        ))}
      </ul>
      {(hasFallbackOnly || hasNativeUnknown) && (
        <div
          role="status"
          className="space-y-1 rounded border border-signal-amber/30 bg-signal-amber/10 px-2.5 py-2 text-xs text-signal-amber"
        >
          {hasFallbackOnly && (
            <p>
              One or more Jira native uploads were unavailable; hosted fallback links were included in the issue body. Do not resubmit, or you will create a duplicate issue.
            </p>
          )}
          {hasNativeUnknown && (
            <p>
              Jira native upload outcome is unknown (timed out or unconfirmed); hosted fallback links were included in the issue body. Never resubmit, or you will create a duplicate issue.
            </p>
          )}
        </div>
      )}
    </div>
  )
}
