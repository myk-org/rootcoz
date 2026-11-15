import type { ReactNode } from 'react'
import { useNavigate } from 'react-router-dom'
import { Building2, ChevronDown, Star } from 'lucide-react'
import { Button } from '@/components/ui/button'
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from '@/components/ui/collapsible'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import { notifyPresetApplied, type FilterPresetPage } from '@/lib/filterPresets'
import type {
  Disclosure,
  Expansion,
  PresetEntry,
  PresetGroups,
} from '@/lib/useSidebarSavedFilters'
import { cn } from '@/lib/utils'

interface SavedFiltersSectionProps {
  page: FilterPresetPage
  navLink: ReactNode
  groups: PresetGroups
  expanded: Expansion
  onToggle: (choice: Disclosure) => void
  /** Expanded presentation shows names and disclosures; collapsed shows icon-only tooltips. */
  collapsed: boolean
  /** Called after navigating so the mobile sidebar closes (the layout only
   * closes on pathname changes, not same-page query changes). */
  onNavigate?: () => void
}

const PAGE_META: Record<
  FilterPresetPage,
  {
    label: string
    disclosure: Disclosure
    scopes: readonly [
      { key: 'server'; label: 'Server'; disclosure: Disclosure },
      { key: 'user'; label: 'User'; disclosure: Disclosure },
    ]
  }
> = {
  dashboard: {
    label: 'Dashboard',
    disclosure: 'Dashboard',
    scopes: [
      { key: 'server', label: 'Server', disclosure: 'DashboardServer' },
      { key: 'user', label: 'User', disclosure: 'DashboardUser' },
    ],
  },
  reports: {
    label: 'Reports',
    disclosure: 'Reports',
    scopes: [
      { key: 'server', label: 'Server', disclosure: 'ReportsServer' },
      { key: 'user', label: 'User', disclosure: 'ReportsUser' },
    ],
  },
}

/**
 * Controlled single-page saved-filter subtree rendered around a page's actual
 * navigation link (`Dashboard` or `Reports`). Read-only — managing presets
 * stays in the filter-bar `Presets` dropdown on each page.
 */
export function SavedFiltersSection({
  page,
  navLink,
  groups,
  expanded,
  onToggle,
  collapsed,
  onNavigate,
}: SavedFiltersSectionProps) {
  const navigate = useNavigate()
  const hasEntries = groups.server.length > 0 || groups.user.length > 0

  if (!hasEntries) {
    return <>{navLink}</>
  }

  const renderItem = (entry: PresetEntry) => {
    const { preset } = entry
    const Icon = preset.scope === 'server' ? Building2 : Star
    const label = preset.name
    const shortcut = (
      <Button
        variant="ghost"
        data-testid={`sidebar-filter-preset-${preset.id}`}
        aria-label={collapsed ? label : undefined}
        onClick={() => {
          // Tell the target page's presets menu which preset is now active.
          notifyPresetApplied(preset.id, preset.page)
          navigate(entry.to)
          onNavigate?.()
        }}
        className={cn(
          'h-auto min-w-0 w-full justify-start gap-2 px-2 py-1.5 text-left text-sm font-normal text-text-secondary [&_svg]:size-3.5',
          collapsed && 'justify-center px-0 py-2',
        )}
      >
        <Icon aria-hidden="true" className="shrink-0 text-text-tertiary" />
        {!collapsed && <span className="truncate">{label}</span>}
      </Button>
    )
    if (collapsed) {
      return (
        <Tooltip key={preset.id}>
          <TooltipTrigger asChild>{shortcut}</TooltipTrigger>
          <TooltipContent side="right">{label}</TooltipContent>
        </Tooltip>
      )
    }
    return <div key={preset.id}>{shortcut}</div>
  }

  // Narrow icon rail temporarily exposes all permitted preset icons beneath the
  // page link without writing or resetting saved disclosure preferences.
  if (collapsed) {
    return (
      <div data-testid={`sidebar-preset-group-${page}`} className="space-y-0.5">
        {navLink}
        {groups.server.map(renderItem)}
        {groups.user.map(renderItem)}
      </div>
    )
  }

  const meta = PAGE_META[page]
  const isPageExpanded = expanded[meta.disclosure]

  return (
    <Collapsible
      open={isPageExpanded}
      onOpenChange={() => onToggle(meta.disclosure)}
      data-testid={`sidebar-preset-group-${page}`}
    >
      <div className="flex items-center gap-0.5">
        <div className="min-w-0 flex-1">{navLink}</div>
        <CollapsibleTrigger asChild>
          <Button
            variant="ghost"
            size="icon"
            aria-label={`${meta.label} saved filters`}
            className="h-8 w-8 shrink-0 [&_svg]:size-3.5"
          >
            <ChevronDown
              aria-hidden="true"
              className={cn(
                'transition-transform duration-150',
                !isPageExpanded && '-rotate-90',
              )}
            />
          </Button>
        </CollapsibleTrigger>
      </div>

      <CollapsibleContent
        forceMount
        hidden={!isPageExpanded}
        className={cn(
          'ml-5 mt-1 mb-1 space-y-1 border-l border-border-default pl-2',
          !isPageExpanded && 'hidden',
        )}
      >
        {meta.scopes.map((scope) => {
          const list = groups[scope.key]
          if (list.length === 0) return null
          const isScopeExpanded = expanded[scope.disclosure]
          return (
            <Collapsible
              key={scope.key}
              open={isScopeExpanded}
              onOpenChange={() => onToggle(scope.disclosure)}
            >
              <CollapsibleTrigger asChild>
                <Button
                  variant="ghost"
                  size="sm"
                  aria-label={`${meta.label} ${scope.label} saved filters`}
                  className="h-7 w-full justify-between px-2 py-1 text-[10px] font-semibold uppercase tracking-wide text-text-tertiary hover:text-text-primary [&_svg]:size-3"
                >
                  <span>{scope.label}</span>
                  <ChevronDown
                    aria-hidden="true"
                    className={cn(
                      'text-text-tertiary transition-transform duration-150',
                      !isScopeExpanded && '-rotate-90',
                    )}
                  />
                </Button>
              </CollapsibleTrigger>
              <CollapsibleContent
                forceMount
                hidden={!isScopeExpanded}
                className={cn('ml-2 space-y-0.5', !isScopeExpanded && 'hidden')}
              >
                {list.map(renderItem)}
              </CollapsibleContent>
            </Collapsible>
          )
        })}
      </CollapsibleContent>
    </Collapsible>
  )
}
