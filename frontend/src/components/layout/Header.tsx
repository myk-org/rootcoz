import { useState, useEffect } from 'react'
import { Link, useLocation } from 'react-router-dom'
import { BookOpen, Menu, MessageSquarePlus, Plus, X } from 'lucide-react'
import { UserBadge } from './UserBadge'
import { FeedbackDialog } from '@/components/shared/FeedbackDialog'
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from '@/components/ui/tooltip'
import { useAuth } from '@/lib/auth'
import { api } from '@/lib/api'
import { cn } from '@/lib/utils'

interface HeaderProps {
  mobileOpen: boolean
  onMobileToggle: () => void
}

export function Header({ mobileOpen, onMobileToggle }: HeaderProps) {
  const location = useLocation()
  const { isOperator } = useAuth()
  const canSubmitAnalysis = isOperator
  const [feedbackOpen, setFeedbackOpen] = useState(false)
  const [feedbackEnabled, setFeedbackEnabled] = useState(false)

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

  return (
    <header data-testid="app-header" className="sticky top-0 z-50 border-b border-border-default bg-surface-card/95 backdrop-blur-sm">
      <div className="flex h-14 items-center justify-between px-4 sm:px-6 lg:px-8">
        <div className="flex items-center gap-4">
          {/* Mobile menu toggle — visible only on small screens */}
          <button
            type="button"
            data-testid="mobile-menu-toggle"
            onClick={onMobileToggle}
            className="rounded-md p-1.5 text-text-secondary hover:bg-surface-hover hover:text-text-primary transition-colors md:hidden"
            aria-label={mobileOpen ? 'Close menu' : 'Open menu'}
          >
            {mobileOpen ? <X className="h-5 w-5" /> : <Menu className="h-5 w-5" />}
          </button>
          <Link
            to="/"
            className="font-display text-lg font-bold tracking-tight text-text-primary"
          >
            RootCoz
          </Link>
          {canSubmitAnalysis && (
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
          )}
        </div>
        <div className="flex items-center gap-3">
          <TooltipProvider delayDuration={200}>
            <Tooltip>
              <TooltipTrigger asChild>
                <a
                  href="https://myk-org.github.io/rootcoz/"
                  target="_blank"
                  rel="noopener noreferrer"
                  className="flex items-center gap-1.5 rounded-md px-2.5 py-1 text-xs font-medium text-text-tertiary transition-colors duration-150 hover:bg-surface-hover hover:text-text-secondary"
                >
                  <BookOpen className="h-4 w-4 shrink-0" />
                  Guide
                </a>
              </TooltipTrigger>
              <TooltipContent>User Guide</TooltipContent>
            </Tooltip>
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
