import { useCallback, useEffect, useRef, useState } from 'react'
import { Link, useLocation } from 'react-router-dom'
import { BookOpen, MessageSquarePlus, Plus, type LucideIcon } from 'lucide-react'
import { UserBadge } from './UserBadge'
import { FeedbackDialog } from '@/components/shared/FeedbackDialog'
import { NavBadge } from '@/components/shared/NavBadge'
import { useAuth } from '@/lib/auth'
import { api } from '@/lib/api'
import { cn } from '@/lib/utils'

interface ExternalNavLink {
  href: string
  label: string
  title: string
  icon: LucideIcon
}

const EXTERNAL_NAV_LINKS: ExternalNavLink[] = [
  { href: 'https://myk-org.github.io/jenkins-job-insight/', label: 'User Guide', title: 'User Guide', icon: BookOpen },
]

const BASE_NAV_LINKS = [
  { to: '/', label: 'Dashboard' },
  { to: '/history', label: 'History' },
]

const UNREAD_POLL_INTERVAL = 30_000

export function NavBar() {
  const location = useLocation()
  const { isAdmin, username } = useAuth()
  const [unreadCount, setUnreadCount] = useState(0)
  const [activeCount, setActiveCount] = useState(0)
  const [feedbackOpen, setFeedbackOpen] = useState(false)
  const [feedbackEnabled, setFeedbackEnabled] = useState(false)
  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null)
  const activeIntervalRef = useRef<ReturnType<typeof setInterval> | null>(null)

  const fetchUnread = useCallback(async () => {
    try {
      const res = await api.get<{ count: number }>('/api/users/mentions/unread-count')
      setUnreadCount(res.count)
    } catch {
      // best-effort
    }
  }, [])

  const fetchActiveCount = useCallback(async (ignore?: { current: boolean }) => {
    try {
      const res = await api.get<{ count: number }>('/api/dashboard/active-count')
      if (ignore?.current) return
      setActiveCount(res.count)
    } catch {
      // best-effort
    }
  }, [])

  useEffect(() => {
    if (!username) return
    fetchUnread()
    intervalRef.current = setInterval(fetchUnread, UNREAD_POLL_INTERVAL)
    return () => {
      if (intervalRef.current) clearInterval(intervalRef.current)
    }
  }, [username, fetchUnread])

  useEffect(() => {
    if (!username) return
    const ignore = { current: false }
    fetchActiveCount(ignore)
    activeIntervalRef.current = setInterval(() => fetchActiveCount(ignore), UNREAD_POLL_INTERVAL)
    return () => {
      ignore.current = true
      if (activeIntervalRef.current) clearInterval(activeIntervalRef.current)
    }
  }, [username, fetchActiveCount])

  useEffect(() => {
    if (!username) return
    function handleMentionsUpdated() {
      fetchUnread()
    }
    window.addEventListener('mentions-updated', handleMentionsUpdated)
    return () => window.removeEventListener('mentions-updated', handleMentionsUpdated)
  }, [username, fetchUnread])

  useEffect(() => {
    if (!username) return
    function handleVisibility() {
      if (document.visibilityState === 'visible') {
        fetchUnread()
        fetchActiveCount()
      }
    }
    document.addEventListener('visibilitychange', handleVisibility)
    return () => document.removeEventListener('visibilitychange', handleVisibility)
  }, [username, fetchUnread, fetchActiveCount])

  // Clear stale counts when user is logged out
  useEffect(() => {
    if (!username) {
      setUnreadCount(0)
      setActiveCount(0)
    }
  }, [username])

  // Fetch server capabilities to check if feedback is enabled
  useEffect(() => {
    let cancelled = false
    async function loadCapabilities(retry = true) {
      try {
        const caps = await api.get<{ feedback_enabled?: boolean }>('/api/capabilities')
        if (!cancelled) setFeedbackEnabled(caps.feedback_enabled ?? false)
      } catch {
        if (!cancelled && retry) {
          setTimeout(() => { if (!cancelled) void loadCapabilities(false) }, 5000)
        } else if (!cancelled) {
          setFeedbackEnabled(false)
        }
      }
    }
    void loadCapabilities()
    return () => {
      cancelled = true
    }
  }, [])

  const baseNavLinks = username
    ? [...BASE_NAV_LINKS, { to: '/mentions', label: 'Mentions' }]
    : BASE_NAV_LINKS

  const navLinks = isAdmin
    ? [...baseNavLinks, { to: '/admin/users', label: 'Users' }, { to: '/admin/token-usage', label: 'Token Usage' }]
    : baseNavLinks

  return (
    <header className="sticky top-0 z-50 border-b border-border-default bg-surface-card/95 backdrop-blur-sm">
      <div className="mx-auto flex h-14 max-w-[1400px] items-center justify-between px-4 sm:px-6 lg:px-8">
        <div className="flex items-center gap-6">
          <Link
            to="/"
            className="font-display text-lg font-bold tracking-tight text-text-primary"
          >
            JJI
          </Link>
          <nav className="flex items-center gap-1">
            {navLinks.map(({ to, label }) => (
              <Link
                key={to}
                to={to}
                className={cn(
                  'relative rounded-md px-3 py-1.5 text-sm font-medium transition-colors duration-150',
                  (location.pathname === to || (to !== '/' && location.pathname.startsWith(to)))
                    ? 'bg-surface-elevated text-text-primary'
                    : 'text-text-secondary hover:bg-surface-hover hover:text-text-primary',
                )}
              >
                {label}
                {to === '/' && <NavBadge count={activeCount} color="orange" tooltip={`${activeCount} ${activeCount === 1 ? 'analysis' : 'analyses'} running`} pulse />}
                {to === '/mentions' && <NavBadge count={unreadCount} color="blue" tooltip={`${unreadCount} unread ${unreadCount === 1 ? 'mention' : 'mentions'}`} />}
              </Link>
            ))}
            <div className="h-6 w-px bg-border-default" />
            <Link
              to="/new-analysis"
              className={cn(
                'flex items-center gap-1.5 rounded-md px-3 py-1.5 text-sm font-medium transition-colors duration-150',
                location.pathname === '/new-analysis'
                  ? 'bg-surface-elevated text-text-primary'
                  : 'text-text-secondary hover:bg-surface-hover hover:text-text-primary',
              )}
            >
              <Plus className="h-3.5 w-3.5" />
              New Analysis
            </Link>
          </nav>
        </div>
        <div className="flex items-center gap-3">
          {EXTERNAL_NAV_LINKS.map(({ href, label, title, icon: Icon }) => (
            <a
              key={href}
              href={href}
              target="_blank"
              rel="noopener noreferrer"
              title={title}
              className="flex items-center gap-1.5 rounded-md px-2.5 py-1 text-xs font-medium text-text-tertiary transition-colors duration-150 hover:bg-surface-hover hover:text-text-secondary"
            >
              <Icon className="h-4 w-4 shrink-0" />
              {label}
            </a>
          ))}
          {feedbackEnabled && (
            <button
              type="button"
              onClick={() => setFeedbackOpen(true)}
              title="Send feedback"
              className="flex items-center gap-1.5 rounded-md px-2.5 py-1 text-xs font-medium text-text-tertiary transition-colors duration-150 hover:bg-surface-hover hover:text-text-secondary"
            >
              <MessageSquarePlus className="h-4 w-4 shrink-0" />
              Feedback
            </button>
          )}
          <UserBadge />
          {feedbackEnabled && (
            <FeedbackDialog open={feedbackOpen} onOpenChange={setFeedbackOpen} />
          )}
        </div>
      </div>
    </header>
  )
}
