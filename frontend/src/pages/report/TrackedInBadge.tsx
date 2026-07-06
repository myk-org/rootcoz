import { useState } from 'react'
import { api, extractApiDetail } from '@/lib/api'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Tooltip, TooltipTrigger, TooltipContent } from '@/components/ui/tooltip'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { isSafeHref } from '@/lib/autoLink'
import { useReportDispatch } from './ReportContext'
import { ExternalLink, Link2 } from 'lucide-react'

/** Detect tracker type from URL string patterns (no HTTP requests). */
export function detectTrackerType(url: string): string {
  const lower = url.toLowerCase()
  if (lower.includes('github.com')) return 'github'
  if (lower.includes('jira') || lower.includes('atlassian')) return 'jira'
  return ''
}

export function TrackerIcon({ type }: { type: string }) {
  if (type === 'github') {
    return (
      <svg className="h-3 w-3" viewBox="0 0 16 16" fill="currentColor">
        <path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.013 8.013 0 0016 8c0-4.42-3.58-8-8-8z" />
      </svg>
    )
  }
  if (type === 'jira') {
    return (
      <svg className="h-3 w-3" viewBox="0 0 24 24" fill="currentColor">
        <path d="M11.571 11.513H0a5.218 5.218 0 0 0 5.232 5.215h2.13v2.057A5.215 5.215 0 0 0 12.593 24V12.518a1.005 1.005 0 0 0-1.022-1.005zM5.024 5.769a5.218 5.218 0 0 0 5.228 5.215h2.133v2.057a5.215 5.215 0 0 0 5.229 5.213V6.774a1.005 1.005 0 0 0-1.022-1.005H5.024zM17.615 0a5.218 5.218 0 0 0 5.229 5.215V1.005A1.005 1.005 0 0 0 21.822 0h-4.207z" />
      </svg>
    )
  }
  return <Link2 className="h-3 w-3" />
}

interface TrackedInBadgeProps {
  url: string
  type: string
}

export function TrackedInBadge({ url, type }: TrackedInBadgeProps) {
  const label = type === 'jira' ? 'Jira' : type === 'github' ? 'GitHub' : 'Tracked'
  const safe = isSafeHref(url)
  const badgeClass = "inline-flex items-center gap-1 rounded-md bg-accent-blue/10 px-2 py-0.5 text-[10px] font-medium text-accent-blue transition-colors"
  const content = (
    <>
      <TrackerIcon type={type} />
      {label}
      {safe && <ExternalLink className="h-2.5 w-2.5" />}
    </>
  )
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        {safe ? (
          <a
            href={url}
            target="_blank"
            rel="noopener noreferrer"
            className={`${badgeClass} hover:bg-accent-blue/20`}
            onClick={(e) => e.stopPropagation()}
          >
            {content}
          </a>
        ) : (
          <span className={badgeClass}>{content}</span>
        )}
      </TooltipTrigger>
      <TooltipContent className="max-w-sm break-all">{url}</TooltipContent>
    </Tooltip>
  )
}

interface TrackInDialogProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  jobId: string
  testName: string
  /** Composite key (reviewKey format) for tracked-in state dispatch. */
  trackedInKey: string
  childJobName?: string
  childBuildNumber?: number
}

export function TrackInDialog({ open, onOpenChange, jobId, testName, trackedInKey, childJobName, childBuildNumber }: TrackInDialogProps) {
  const dispatch = useReportDispatch()
  const [url, setUrl] = useState('')
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')

  const detectType = detectTrackerType

  async function handleSave() {
    const trimmed = url.trim()
    if (!trimmed) return
    // Client-side URL validation
    try {
      const parsed = new URL(trimmed)
      if (!['http:', 'https:'].includes(parsed.protocol)) {
        setError('URL must start with http:// or https://')
        return
      }
    } catch {
      setError('Please enter a valid URL (e.g., https://github.com/org/repo/issues/123)')
      return
    }
    setSaving(true)
    setError('')
    try {
      const res = await api.put<{ tracked_in_url: string; tracked_in_type: string; tracked_in_by: string }>(
        `/results/${jobId}/tracked-in`,
        { test_name: testName, url: trimmed, type: detectType(url), child_job_name: childJobName ?? '', child_build_number: childBuildNumber ?? 0 },
      )
      dispatch({
        type: 'SET_TRACKED_IN_ENTRY',
        payload: { testName: trackedInKey, entry: { tracked_in_url: res.tracked_in_url, tracked_in_type: res.tracked_in_type, tracked_in_by: res.tracked_in_by } },
      })
      onOpenChange(false)
      setUrl('')
    } catch (err) {
      const detail = extractApiDetail(err)
      if (detail?.includes('URL must use') || detail?.includes('URL must include')) {
        setError('Please enter a valid URL starting with http:// or https://')
      } else {
        setError(detail ?? (err instanceof Error ? err.message : 'Failed to add tracked link'))
      }
    } finally {
      setSaving(false)
    }
  }

  return (
    <Dialog open={open} onOpenChange={(o) => { if (!saving) { onOpenChange(o); if (!o) { setUrl(''); setError('') } } }}>
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle>Track In</DialogTitle>
          <DialogDescription>Link this failure to an existing Jira or GitHub issue.</DialogDescription>
        </DialogHeader>
        <div className="space-y-3">
          <div className="space-y-1.5">
            <label htmlFor="tracked-in-url" className="text-xs font-display uppercase tracking-widest text-text-tertiary">Issue URL</label>
            <Input
              id="tracked-in-url"
              value={url}
              onChange={(e) => setUrl(e.target.value)}
              placeholder="https://jira.example.com/browse/PROJ-123"
              className="text-sm"
              onKeyDown={(e) => { if (e.key === 'Enter' && url.trim() && !saving) handleSave() }}
            />
          </div>
          {url.trim() && (
            <div className="flex items-center gap-2">
              <span className="text-xs text-text-tertiary">Detected:</span>
              <Badge variant="outline" className="text-[10px]">
                <TrackerIcon type={detectType(url)} />
                <span className="ml-1">{detectType(url) || 'unknown'}</span>
              </Badge>
            </div>
          )}
          {error && <p className="text-xs text-signal-red">{error}</p>}
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)} disabled={saving}>Cancel</Button>
          <Button onClick={handleSave} disabled={!url.trim() || saving}>
            {saving ? 'Saving...' : 'Save'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
