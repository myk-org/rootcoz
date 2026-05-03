import { useCallback, useEffect, useState } from 'react'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Button } from '@/components/ui/button'
import { subscribeToPush, getVapidPublicKey, hasActivePushSubscription, getPushSubscriptionState } from '@/lib/notifications'
import { Bell } from 'lucide-react'

const ASKED_KEY = 'jji_notifications_asked'
const SHOW_DELAY_MS = 1500

function wasAlreadyAsked(): boolean {
  try {
    return localStorage.getItem(ASKED_KEY) === 'true'
  } catch {
    return true // If storage unavailable, don't prompt
  }
}

function markAsked(): void {
  try {
    localStorage.setItem(ASKED_KEY, 'true')
  } catch {
    // Storage unavailable — silently ignore
  }
}

export function NotificationPrompt() {
  const [open, setOpen] = useState(false)
  const [enabling, setEnabling] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    const alreadyAsked = wasAlreadyAsked()
    if (alreadyAsked) return

    let cancelled = false

    async function check() {
      const state = await getPushSubscriptionState()
      if (cancelled || state === 'unsupported') return

      // Check for actual push subscription, not just permission
      if (await hasActivePushSubscription()) {
        markAsked()
        return
      }

      const vapidKey = await getVapidPublicKey()
      if (cancelled || !vapidKey) return

      // All conditions met — show after delay
      setTimeout(() => {
        if (!cancelled) setOpen(true)
      }, SHOW_DELAY_MS)
    }

    check()
    return () => { cancelled = true }
  }, [])

  const handleEnable = useCallback(async () => {
    setEnabling(true)
    const result = await subscribeToPush()
    setEnabling(false)
    if (result.ok) {
      markAsked()
      setOpen(false)
    } else {
      setError(result.error || 'Failed to enable notifications')
    }
  }, [])

  const handleDismiss = useCallback(() => {
    markAsked()
    setOpen(false)
  }, [])

  return (
    <Dialog open={open} onOpenChange={(v) => { if (!v) handleDismiss() }}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <Bell className="h-5 w-5 text-accent-blue" />
            Enable Notifications?
          </DialogTitle>
          <DialogDescription>
            Get notified when someone mentions you in a comment.
          </DialogDescription>
          {error && (
            <p className="text-sm text-destructive mt-2">{error}</p>
          )}
        </DialogHeader>
        <DialogFooter>
          <Button variant="ghost" onClick={handleDismiss} disabled={enabling}>
            Not now
          </Button>
          <Button onClick={handleEnable} disabled={enabling}>
            {enabling ? 'Enabling…' : 'Enable'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
