import { useEffect, useState } from 'react'
import { Outlet, useLocation } from 'react-router-dom'
import { Header } from './Header'
import { Sidebar } from './Sidebar'
import { useAuth } from '@/lib/auth'
import { api } from '@/lib/api'

export function Layout() {
  const { username, isAdmin } = useAuth()
  const location = useLocation()
  const [unreadCount, setUnreadCount] = useState(0)
  const [activeCount, setActiveCount] = useState(0)
  const [pendingCount, setPendingCount] = useState(0)
  const [mobileOpen, setMobileOpen] = useState(false)

  // Close mobile sidebar on route change
  useEffect(() => {
    setMobileOpen(false)
  }, [location.pathname])

  // Unified SSE stream for navbar badges — server pushes both counts in real-time
  useEffect(() => {
    if (!username) return

    const eventSource = new EventSource('/api/navbar/stream')

    eventSource.addEventListener('active-count', (event) => {
      const count = parseInt(event.data, 10)
      if (!isNaN(count)) setActiveCount(count)
    })

    eventSource.addEventListener('unread-count', (event) => {
      const count = parseInt(event.data, 10)
      if (!isNaN(count)) setUnreadCount(count)
    })

    eventSource.onerror = () => {
      // EventSource auto-reconnects on error
    }

    return () => {
      eventSource.close()
    }
  }, [username])

  // Fetch pending user count for admin badge
  useEffect(() => {
    if (!isAdmin) return
    api.get<{ users: { username: string }[] }>('/api/admin/users/pending')
      .then(res => setPendingCount(res.users?.length ?? 0))
      .catch(() => setPendingCount(0))
  }, [isAdmin])

  // Clear stale counts when user is logged out
  useEffect(() => {
    if (!username) {
      setUnreadCount(0)
      setActiveCount(0)
      setPendingCount(0)
    }
  }, [username])

  return (
    <div className="flex h-screen flex-col bg-surface-page">
      <Header mobileOpen={mobileOpen} onMobileToggle={() => setMobileOpen(prev => !prev)} />
      <div className="flex flex-1 overflow-hidden">
        <Sidebar
          badges={{ activeCount, unreadCount, pendingCount }}
          mobileOpen={mobileOpen}
          onMobileClose={() => setMobileOpen(false)}
        />
        <main className="flex-1 overflow-y-auto px-4 py-6 sm:px-6 lg:px-8">
          <div className="mx-auto max-w-[1400px]">
            <Outlet />
          </div>
        </main>
      </div>
    </div>
  )
}
