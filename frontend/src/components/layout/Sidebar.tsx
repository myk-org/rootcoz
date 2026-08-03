import type { MouseEvent as ReactMouseEvent } from 'react'
import { useCallback, useEffect, useRef, useState } from 'react'
import { Link, useLocation } from 'react-router-dom'
import {
  AtSign,
  BarChart3,
  Clock,
  Coins,
  LayoutDashboard,
  MessageSquare,
  ScrollText,
  Settings,
  Users,
  type LucideIcon,
} from 'lucide-react'
import { NavBadge } from '@/components/shared/NavBadge'
import { Separator } from '@/components/ui/separator'
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from '@/components/ui/tooltip'
import { api } from '@/lib/api'
import { useAuth } from '@/lib/auth'
import { cn } from '@/lib/utils'

// ─── Constants ──────────────────────────────────────────────────────

const LS_WIDTH_KEY = 'rootcoz_sidebar_width'
const LS_COLLAPSED_KEY = 'rootcoz_sidebar_collapsed'
const DEFAULT_WIDTH = 200
const MIN_WIDTH = 48
const MAX_WIDTH = 320
/** Below this width the sidebar auto-collapses to icon-only mode. */
const COLLAPSE_THRESHOLD = 100

interface NavItem {
  to: string
  label: string
  icon: LucideIcon
}

const USER_NAV_ITEMS: NavItem[] = [
  { to: '/', label: 'Dashboard', icon: LayoutDashboard },
  { to: '/history', label: 'History', icon: Clock },
]

const MENTIONS_ITEM: NavItem = { to: '/mentions', label: 'Mentions', icon: AtSign }

const REPORTS_ITEM: NavItem = { to: '/reports', label: 'Reports', icon: BarChart3 }

const ADMIN_NAV_ITEMS: NavItem[] = [
  { to: '/admin/users', label: 'Users', icon: Users },
  { to: '/admin/token-usage', label: 'Tokens', icon: Coins },
  { to: '/admin/chat', label: 'Chat', icon: MessageSquare },
  { to: '/admin/logs', label: 'Logs', icon: ScrollText },
  { to: '/admin/settings', label: 'Settings', icon: Settings },
]

// ─── Badges ─────────────────────────────────────────────────────────

interface SidebarBadges {
  activeCount: number
  unreadCount: number
  pendingCount: number
}

// ─── Component ──────────────────────────────────────────────────────

interface SidebarProps {
  badges: SidebarBadges
  /** Whether the mobile overlay sidebar is open. */
  mobileOpen: boolean
  /** Callback to close the mobile sidebar. */
  onMobileClose: () => void
}

export function Sidebar({ badges, mobileOpen, onMobileClose }: SidebarProps) {
  const location = useLocation()
  const { isAdmin, canViewReports, role, username, loading, authenticated } = useAuth()

  // ─── Width / collapse state ─────────────────────────────────────
  const [collapsed, setCollapsed] = useState(() => {
    try {
      return localStorage.getItem(LS_COLLAPSED_KEY) === 'true'
    } catch {
      return false
    }
  })
  const [width, setWidth] = useState(() => {
    try {
      const saved = localStorage.getItem(LS_WIDTH_KEY)
      if (!saved) return DEFAULT_WIDTH
      const n = Number(saved)
      return Number.isFinite(n) ? Math.max(MIN_WIDTH, Math.min(MAX_WIDTH, n)) : DEFAULT_WIDTH
    } catch {
      return DEFAULT_WIDTH
    }
  })

  // Persist collapsed state
  useEffect(() => {
    try { localStorage.setItem(LS_COLLAPSED_KEY, String(collapsed)) } catch { /* storage unavailable */ }
  }, [collapsed])

  // ─── Server version ─────────────────────────────────────────────
  const [version, setVersion] = useState('')

  useEffect(() => {
    if (loading || !authenticated) return

    const controller = new AbortController()

    async function fetchVersion() {
      try {
        const data = await api.get<{ version: string }>('/api/version', { signal: controller.signal })
        if (!data.version) return
        setVersion(data.version)
      } catch (err) {
        if (err instanceof DOMException && err.name === 'AbortError') return
        console.debug('Failed to fetch server version:', err)
      }
    }

    fetchVersion()
    return () => { controller.abort() }
  }, [loading, authenticated])

  // ─── Drag resize ────────────────────────────────────────────────
  const [isDragging, setIsDragging] = useState(false)
  const startX = useRef(0)
  const startW = useRef(width)
  const widthRef = useRef(width)

  // Keep widthRef in sync so mouseup can read the latest value without a dependency
  useEffect(() => { widthRef.current = width }, [width])

  const onMouseDown = useCallback((e: ReactMouseEvent) => {
    e.preventDefault()
    startX.current = e.clientX
    startW.current = collapsed ? MIN_WIDTH : widthRef.current
    document.body.style.cursor = 'col-resize'
    document.body.style.userSelect = 'none'
    setIsDragging(true)
  }, [collapsed])

  // Attach listeners only while dragging — no churn, no idle listeners
  useEffect(() => {
    if (!isDragging) return

    function onMouseMove(e: MouseEvent) {
      const newWidth = Math.max(MIN_WIDTH, Math.min(MAX_WIDTH, startW.current + (e.clientX - startX.current)))
      if (newWidth <= COLLAPSE_THRESHOLD) {
        setCollapsed(true)
        // Don't overwrite width — keep the user's last expanded width
      } else {
        setCollapsed(false)
        setWidth(newWidth)
        widthRef.current = newWidth
      }
    }

    function onMouseUp() {
      document.body.style.cursor = ''
      document.body.style.userSelect = ''
      // Persist width only at end of drag (not on every mousemove)
      try { localStorage.setItem(LS_WIDTH_KEY, String(widthRef.current)) } catch { /* storage unavailable */ }
      setIsDragging(false)
    }

    document.addEventListener('mousemove', onMouseMove)
    document.addEventListener('mouseup', onMouseUp)
    return () => {
      document.removeEventListener('mousemove', onMouseMove)
      document.removeEventListener('mouseup', onMouseUp)
      // Always restore body styles on cleanup (handles mid-drag unmount)
      document.body.style.cursor = ''
      document.body.style.userSelect = ''
    }
  }, [isDragging])

  // ─── Build nav items ────────────────────────────────────────────
  const showMentions = !!username && !loading && role !== 'viewer'
  const userItems = showMentions ? [...USER_NAV_ITEMS, MENTIONS_ITEM] : USER_NAV_ITEMS

  const isActive = (to: string) =>
    to === '/' ? location.pathname === '/' : location.pathname.startsWith(to)

  const effectiveWidth = collapsed ? MIN_WIDTH : width

  const renderItem = (item: NavItem) => {
    const active = isActive(item.to)
    const Icon = item.icon
    const badge = getBadge(item.to, badges)

    const link = (
      <Link
        to={item.to}
        className={cn(
          'relative flex items-center gap-3 rounded-md px-3 py-2 text-sm font-medium transition-colors duration-150',
          collapsed && 'justify-center px-0',
          active
            ? 'bg-surface-elevated text-text-primary'
            : 'text-text-secondary hover:bg-surface-hover hover:text-text-primary',
        )}
      >
        <Icon className="h-4 w-4 shrink-0" />
        {!collapsed && <span className="truncate">{item.label}</span>}
        {badge}
      </Link>
    )

    if (collapsed) {
      return (
        <Tooltip key={item.to}>
          <TooltipTrigger asChild>{link}</TooltipTrigger>
          <TooltipContent side="right">{item.label}</TooltipContent>
        </Tooltip>
      )
    }

    return <div key={item.to}>{link}</div>
  }

  const sidebarContent = (
    <nav className="flex flex-1 flex-col gap-1 overflow-y-auto px-2 py-3">
      {userItems.map(renderItem)}

      {!loading && (canViewReports || isAdmin) && (
        <>
          <Separator className="my-2" />
          {(canViewReports || isAdmin) && renderItem(REPORTS_ITEM)}
          {isAdmin && ADMIN_NAV_ITEMS.map(renderItem)}
        </>
      )}
    </nav>
  )

  const versionFooter = version ? (
    <div data-testid="sidebar-version" className="border-t border-border-default px-3 py-2">
      {collapsed ? (
        <Tooltip>
          <TooltipTrigger asChild>
            <span className="block text-center text-[10px] text-text-tertiary font-mono">v</span>
          </TooltipTrigger>
          <TooltipContent side="right">v{version}</TooltipContent>
        </Tooltip>
      ) : (
        <span className="text-[10px] text-text-tertiary font-mono">v{version}</span>
      )}
    </div>
  ) : null

  return (
    <TooltipProvider delayDuration={200}>
      {/* ─── Desktop sidebar ─────────────────────────────────────── */}
      <aside
        data-testid="app-sidebar"
        className="relative hidden shrink-0 flex-col border-r border-border-default bg-surface-card md:flex"
        style={{ width: effectiveWidth }}
      >
        {sidebarContent}
        {versionFooter}

        {/* Drag handle */}
        <div
          data-testid="sidebar-resize-handle"
          onMouseDown={onMouseDown}
          className="absolute right-0 top-0 h-full w-1 cursor-col-resize hover:bg-signal-blue/30 active:bg-signal-blue/50 transition-colors"
        />
      </aside>

      {/* ─── Mobile sidebar overlay ──────────────────────────────── */}
      {mobileOpen && (
        <>
          {/* Backdrop */}
          <div
            data-testid="mobile-sidebar-backdrop"
            className="fixed inset-0 z-40 bg-black/60 backdrop-blur-sm md:hidden"
            onClick={onMobileClose}
          />
          {/* Slide-in sidebar */}
          <aside
            data-testid="mobile-sidebar"
            className="fixed inset-y-0 left-0 z-50 flex w-64 flex-col border-r border-border-default bg-surface-card pt-16 md:hidden"
          >
            {sidebarContent}
            {versionFooter}
          </aside>
        </>
      )}
    </TooltipProvider>
  )
}

// ─── Helpers ────────────────────────────────────────────────────────

function getBadge(to: string, badges: SidebarBadges) {
  if (to === '/' && badges.activeCount > 0) {
    return <NavBadge count={badges.activeCount} color="orange" tooltip={`${badges.activeCount} ${badges.activeCount === 1 ? 'analysis' : 'analyses'} running`} pulse />
  }
  if (to === '/mentions' && badges.unreadCount > 0) {
    return <NavBadge count={badges.unreadCount} color="blue" tooltip={`${badges.unreadCount} unread ${badges.unreadCount === 1 ? 'mention' : 'mentions'}`} />
  }
  if (to === '/admin/users' && badges.pendingCount > 0) {
    return <NavBadge count={badges.pendingCount} color="orange" tooltip={`${badges.pendingCount} pending ${badges.pendingCount === 1 ? 'approval' : 'approvals'}`} />
  }
  return null
}
