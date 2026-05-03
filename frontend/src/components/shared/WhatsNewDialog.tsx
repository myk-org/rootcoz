import { useEffect, useState } from 'react'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Button } from '@/components/ui/button'
import { Sparkles } from 'lucide-react'
import changelog from '@/changelog.json'

const LS_KEY = 'jji_last_seen_changelog_version'

/** Changelog sorted by date descending — robust to ordering in changelog.json */
const sortedChangelog = [...changelog].sort((a, b) => b.date.localeCompare(a.date))

function getLatestVersion(): string | null {
  return sortedChangelog.length > 0 ? sortedChangelog[0].version : null
}

function shouldShow(): boolean {
  const latest = getLatestVersion()
  if (!latest) return false
  try {
    const seen = localStorage.getItem(LS_KEY)
    return seen !== latest
  } catch {
    return false
  }
}

export function WhatsNewDialog() {
  const [open, setOpen] = useState(false)
  const [dontShowAgain, setDontShowAgain] = useState(false)

  useEffect(() => {
    if (shouldShow()) {
      setOpen(true)
    }
  }, [])

  function handleDismiss() {
    if (dontShowAgain) {
      const latest = getLatestVersion()
      if (latest) {
        try {
          localStorage.setItem(LS_KEY, latest)
        } catch {
          // localStorage may be unavailable in private browsing
        }
      }
    }
    setOpen(false)
  }

  if (sortedChangelog.length === 0) return null

  const latest = sortedChangelog[0]

  return (
    <Dialog open={open} onOpenChange={(v) => { if (!v) handleDismiss() }}>
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <Sparkles aria-hidden="true" className="h-5 w-5 text-signal-blue" />
            What&apos;s New
          </DialogTitle>
          <DialogDescription>
            Version {latest.version} &mdash; {latest.date}
          </DialogDescription>
        </DialogHeader>

        <ul className="space-y-3 py-2" role="list">
          {latest.entries.map((entry, idx) => (
            <li key={`${latest.version}-${idx}`} className="flex gap-3">
              <span className="mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full bg-signal-blue" />
              <div>
                <p className="text-sm font-medium text-text-primary">{entry.title}</p>
                <p className="text-xs text-text-secondary">{entry.description}</p>
              </div>
            </li>
          ))}
        </ul>

        <DialogFooter className="flex items-center justify-between sm:justify-between">
          <label className="flex items-center gap-2 text-xs text-text-tertiary cursor-pointer select-none">
            <input
              type="checkbox"
              checked={dontShowAgain}
              onChange={(e) => setDontShowAgain(e.target.checked)}
              className="h-3.5 w-3.5 rounded border-border-default accent-signal-blue"
            />
            Don&apos;t show again
          </label>
          <Button size="sm" onClick={handleDismiss}>
            Got it
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
