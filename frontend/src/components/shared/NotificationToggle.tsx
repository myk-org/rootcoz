import { useState, useEffect, useCallback } from 'react'
import {
  getPushSubscriptionState,
  hasActivePushSubscription,
  subscribeToPush,
  unsubscribeFromPush,
} from '@/lib/notifications'
import { Button } from '@/components/ui/button'
import { Bell, BellOff } from 'lucide-react'
import { SectionDivider } from '@/components/shared/SectionDivider'

type PushState = Awaited<ReturnType<typeof getPushSubscriptionState>> | 'loading'

export function NotificationToggle() {
  const [pushState, setPushState] = useState<PushState>('loading')
  const [hasSubscription, setHasSubscription] = useState(false)
  const [toggling, setToggling] = useState(false)
  const [toggleError, setToggleError] = useState<string | null>(null)

  const refreshState = useCallback(async () => {
    try {
      const state = await getPushSubscriptionState()
      setPushState(state)
      setHasSubscription(await hasActivePushSubscription())
    } catch (err) {
      setToggleError(err instanceof Error ? err.message : 'Unable to read notification state')
      setPushState((prev) => (prev === 'loading' ? 'default' : prev))
    }
  }, [])

  useEffect(() => {
    void refreshState()
  }, [refreshState])

  async function handleToggle() {
    setToggling(true)
    setToggleError(null)
    try {
      if (hasSubscription) {
        const ok = await unsubscribeFromPush()
        if (ok) setHasSubscription(false)
        else setToggleError('Failed to disable notifications')
      } else {
        const result = await subscribeToPush()
        if (result.ok) {
          setHasSubscription(true)
        } else {
          setToggleError(result.error || 'Failed to enable notifications')
        }
      }
      await refreshState()
    } catch (err) {
      setToggleError(err instanceof Error ? err.message : 'Unexpected error')
    } finally {
      setToggling(false)
    }
  }

  if (pushState === 'loading') return null

  if (pushState === 'unsupported') {
    return (
      <div className="space-y-1.5 pt-2">
        <SectionDivider title="Push Notifications" />
        <p className="text-xs text-text-tertiary">Push notifications are not supported in this browser.</p>
      </div>
    )
  }

  if (pushState === 'denied') {
    return (
      <div className="space-y-1.5 pt-2">
        <SectionDivider title="Push Notifications" />
        <div className="flex items-center gap-2 text-xs text-signal-amber">
          <BellOff className="h-4 w-4 shrink-0" />
          <span>Notifications blocked. To re-enable, update this site&apos;s notification permission in your browser settings.</span>
        </div>
      </div>
    )
  }

  return (
    <div className="space-y-1.5 pt-2">
      <SectionDivider title="Push Notifications" />
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2 text-xs text-text-secondary">
          {hasSubscription ? <Bell className="h-4 w-4 text-signal-green" /> : <BellOff className="h-4 w-4" />}
          <span>{hasSubscription ? 'Notifications enabled' : 'Notifications disabled'}</span>
        </div>
        <Button type="button" variant={hasSubscription ? 'outline' : 'default'} size="sm" disabled={toggling} onClick={handleToggle}>
          {toggling ? 'Updating...' : hasSubscription ? 'Disable' : 'Enable'}
        </Button>
      </div>
      {toggleError && (
        <p className="text-xs text-signal-red">{toggleError}</p>
      )}
      <p className="text-xs text-text-tertiary">Receive browser notifications when someone mentions you in a comment.</p>
    </div>
  )
}

export type { PushState }
