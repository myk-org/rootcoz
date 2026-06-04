import { useEffect, useState } from 'react'
import { Link, useLocation } from 'react-router-dom'
import { BookOpen, MessageSquarePlus, Plus, type LucideIcon } from 'lucide-react'
import { UserBadge } from './UserBadge'
import { FeedbackDialog } from '@/components/shared/FeedbackDialog'
import { NavBadge } from '@/components/shared/NavBadge'
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from '@/components/ui/tooltip'
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
  { href: 'https://myk-org.github.io/rootcoz/', label: 'Guide', title: 'User Guide', icon: BookOpen },
]

const BASE_NAV_LINKS = [
  { to: '/', label: 'Dashboard' },
  { to: '/history', label: 'History' },
]

export function NavBar() {
  const location = useLocation()
  const { isAdmin, username } = useAuth()
  const [unreadCount, setUnreadCount] = useState(0)
  const [activeCount, setActiveCount] = useState(0)
  const [pendingCount, setPendingCount] = useState(0)
  const [feedbackOpen, setFeedbackOpen] = useState(false)
  const [feedbackEnabled, setFeedbackEnabled] = useState(false)
  // Unified SSE stream for navbar badges — server pushes both counts in real-time
  useEffect(() => {
    if (!username) return

    const eventSource = new EventSource('/api/navbar/stream')

    eventSource.addEventListener('active-count', (event) => {
      const count = parseInt(event.data, 10)
      if (!isNaN(count)) {
        setActiveCount(count)
      }
    })

    eventSource.addEventListener('unread-count', (event) => {
      const count = parseInt(event.data, 10)
      if (!isNaN(count)) {
        setUnreadCount(count)
      }
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
    ? [...baseNavLinks, { to: '/admin/users', label: 'Users' }, { to: '/admin/token-usage', label: 'Tokens' }, { to: '/reports', label: 'Reports' }, { to: '/admin/settings', label: 'Settings' }, { to: '/admin/chat', label: 'Chat' }]
    : baseNavLinks

  return (
    <header className="sticky top-0 z-50 border-b border-border-default bg-surface-card/95 backdrop-blur-sm">
      <div className="mx-auto flex h-14 max-w-[1400px] items-center justify-between px-4 sm:px-6 lg:px-8">
        <div className="flex items-center gap-6">
          <Link
            to="/"
            className="font-display text-lg font-bold tracking-tight text-text-primary"
          >
            RootCoz
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
                {to === '/admin/users' && <NavBadge count={pendingCount} color="orange" tooltip={`${pendingCount} pending ${pendingCount === 1 ? 'approval' : 'approvals'}`} />}
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
          <TooltipProvider delayDuration={200}>
            {EXTERNAL_NAV_LINKS.map(({ href, label, title, icon: Icon }) => (
              <Tooltip key={href}>
                <TooltipTrigger asChild>
                  <a
                    href={href}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="flex items-center gap-1.5 rounded-md px-2.5 py-1 text-xs font-medium text-text-tertiary transition-colors duration-150 hover:bg-surface-hover hover:text-text-secondary"
                  >
                    <Icon className="h-4 w-4 shrink-0" />
                    {label}
                  </a>
                </TooltipTrigger>
                <TooltipContent>{title}</TooltipContent>
              </Tooltip>
            ))}
            {feedbackEnabled && (
              <Tooltip>
                <TooltipTrigger asChild>
                  <button
                    type="button"
                    onClick={() => setFeedbackOpen(true)}
                    className="flex items-center gap-1.5 rounded-md px-2.5 py-1 text-xs font-medium text-text-tertiary transition-colors duration-150 hover:bg-surface-hover hover:text-text-secondary"
                  >
                    <MessageSquarePlus className="h-4 w-4 shrink-0" />
                    Feedback
                  </button>
                </TooltipTrigger>
                <TooltipContent>Send feedback</TooltipContent>
              </Tooltip>
            )}
          </TooltipProvider>
          <UserBadge />
          {feedbackEnabled && (
            <FeedbackDialog open={feedbackOpen} onOpenChange={setFeedbackOpen} />
          )}
        </div>
      </div>
    </header>
  )
}
