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
import { api } from '@/lib/api'

const LS_KEY = 'rootcoz_last_seen_changelog_version'

interface ReleaseData {
  version: string
  name: string
  body: string
  published_at: string
  html_url: string
}

/** Parse GitHub release markdown body into bullet points */
function parseReleaseEntries(body: string): { title: string; description: string }[] {
  const entries: { title: string; description: string }[] = []
  for (const line of body.split('\n')) {
    // Match "- **Title** — Description (#123)" or "- **Title** - Description"
    const match = line.match(/^-\s+\*\*(.+?)\*\*\s*[—\-–]\s*(.+)/)
    if (match) {
      entries.push({ title: match[1].trim(), description: match[2].trim() })
      continue
    }

    // Fallback: any markdown bullet line
    const bullet = line.match(/^-\s+(.+)/)
    if (bullet) {
      entries.push({ title: bullet[1].trim(), description: '' })
    }
  }
  return entries
}

export function WhatsNewDialog() {
  const [open, setOpen] = useState(false)
  const [dontShowAgain, setDontShowAgain] = useState(false)
  const [release, setRelease] = useState<ReleaseData | null>(null)
  const [entries, setEntries] = useState<{ title: string; description: string }[]>([])

  useEffect(() => {
    let cancelled = false

    async function fetchRelease() {
      try {
        const data = await api.get<ReleaseData>('/api/releases/latest')
        if (cancelled || !data.version) return

        // Check if user has already seen this version
        try {
          const seen = localStorage.getItem(LS_KEY)
          if (seen === data.version) return
        } catch {
          return // localStorage unavailable
        }

        const parsed = parseReleaseEntries(data.body)
        if (parsed.length === 0) return // No entries to show

        setRelease(data)
        setEntries(parsed)
        setOpen(true)
      } catch {
        // API unavailable — silently skip
      }
    }

    fetchRelease()
    return () => { cancelled = true }
  }, [])

  function handleDismiss() {
    if (dontShowAgain && release) {
      try {
        localStorage.setItem(LS_KEY, release.version)
      } catch {
        // localStorage may be unavailable
      }
    }
    setOpen(false)
  }

  if (!release) return null

  return (
    <Dialog open={open} onOpenChange={(v) => { if (!v) handleDismiss() }}>
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <Sparkles aria-hidden="true" className="h-5 w-5 text-signal-blue" />
            What&apos;s New
          </DialogTitle>
          <DialogDescription>
            Version {release.version}
          </DialogDescription>
        </DialogHeader>

        <ul className="space-y-3 py-2 max-h-[50vh] overflow-y-auto" role="list">
          {entries.map((entry, idx) => (
            <li key={idx} className="flex gap-3">
              <span className="mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full bg-signal-blue" />
              <div>
                <p className="text-sm font-medium text-text-primary">{entry.title}</p>
                <p className="text-xs text-text-secondary">{entry.description}</p>
              </div>
            </li>
          ))}
        </ul>

        {release.html_url && (
          <p className="text-xs text-text-tertiary">
            <a href={release.html_url} target="_blank" rel="noopener noreferrer" className="text-text-link hover:underline">
              View full release notes →
            </a>
          </p>
        )}

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
