import { useMemo, useState } from 'react'
import { Outlet, useLocation } from 'react-router-dom'
import { Header } from './Header'
import { Sidebar } from './Sidebar'
import { useAuth } from '@/lib/auth'
import { useSSE } from '@/lib/useSSE'
import { useHasMoreBelow } from './useHasMoreBelow'

export function Layout() {
  const { username } = useAuth()
  const location = useLocation()
  const { ref: mainRef, hasMore } = useHasMoreBelow<HTMLElement>()
  const [unreadCount, setUnreadCount] = useState(0)
  const [activeCount, setActiveCount] = useState(0)
  const [pendingCount, setPendingCount] = useState(0)
  const [mobileOpen, setMobileOpen] = useState(false)

  // Close mobile sidebar on route change. Adjusting state during render (the
  // React-recommended "reset state on prop change" pattern) keeps the close
  // in the same commit as the navigation instead of an extra effect pass.
  const [lastPath, setLastPath] = useState(location.pathname)
  if (lastPath !== location.pathname) {
    setLastPath(location.pathname)
    setMobileOpen(false)
  }

  // Multiplexed SSE stream for navbar badges — shared single connection
  const navbarEvents = useMemo(() => ({
    'active-count': (data: string) => {
      const count = parseInt(data, 10)
      if (!isNaN(count)) setActiveCount(count)
    },
    'unread-count': (data: string) => {
      const count = parseInt(data, 10)
      if (!isNaN(count)) setUnreadCount(count)
    },
    'pending-count': (data: string) => {
      const count = parseInt(data, 10)
      if (!isNaN(count)) setPendingCount(count)
    },
  }), [])

  useSSE(username ? 'navbar' : null, navbarEvents)

  // Counts are only meaningful while signed in — derive the zeros for a
  // logged-out user instead of clearing them from an effect. No event can
  // arrive while signed out because the navbar subscription is disabled.
  const badges = {
    activeCount: username ? activeCount : 0,
    unreadCount: username ? unreadCount : 0,
    pendingCount: username ? pendingCount : 0,
  }

  return (
    <div className="flex h-screen flex-col bg-surface-page">
      <Header mobileOpen={mobileOpen} onMobileToggle={() => setMobileOpen(prev => !prev)} />
      <div className="flex flex-1 overflow-hidden">
        <Sidebar
          badges={badges}
          mobileOpen={mobileOpen}
          onMobileClose={() => setMobileOpen(false)}
        />
        <div className="relative min-w-0 flex-1">
          <main ref={mainRef} className="h-full overflow-y-auto px-4 py-6 sm:px-6 lg:px-8">
            <div className="mx-auto max-w-[1400px]">
              <Outlet />
            </div>
          </main>
          {hasMore && (
            <div
              aria-hidden="true"
              className="pointer-events-none absolute inset-x-0 bottom-0 h-8 bg-gradient-to-t from-surface-page to-transparent"
            />
          )}
        </div>
      </div>
    </div>
  )
}
