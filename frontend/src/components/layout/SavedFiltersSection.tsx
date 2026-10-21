import { useEffect, useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { Building2, Star } from 'lucide-react'
import { Tooltip, TooltipTrigger, TooltipContent } from '@/components/ui/tooltip'
import { cn } from '@/lib/utils'
import { useAuth } from '@/lib/auth'
import {
  fetchFilterPresets,
  filtersToQueryString,
  notifyPresetApplied,
  PRESETS_CHANGED_EVENT,
  type FilterPreset,
  type FilterPresetPage,
} from '@/lib/filterPresets'

// ─── Sidebar "Saved filters" section (#179) ─────────────────────────

interface SavedFiltersSectionProps {
  /** Expanded sidebar shows names; collapsed shows icon-only tooltips. */
  collapsed: boolean
  /** Called after navigating so the mobile sidebar closes (the layout only
   * closes on pathname changes, not same-page query changes). */
  onNavigate?: () => void
}

interface PresetEntry {
  preset: FilterPreset
  to: string
}

const PAGE_ROUTES: Record<FilterPresetPage, string> = {
  dashboard: '/',
  reports: '/reports',
}

/**
 * Lists the user's personal presets plus server presets (both pages) in the
 * sidebar. Clicking an entry navigates to the preset's page with its filters
 * applied as URL query params. Read-only — managing presets happens in the
 * filter-bar dropdown on the respective page. Reports presets are only
 * listed for users who can open the reports page.
 */
export function SavedFiltersSection({ collapsed, onNavigate }: SavedFiltersSectionProps) {
  const navigate = useNavigate()
  const { canViewReports } = useAuth()
  const [entries, setEntries] = useState<PresetEntry[] | null>(null)

  // Load presets, refresh on cross-component mutations, and follow the
  // user's reports access. The loader lives inside the effect with a
  // cancellation guard so stale responses never overwrite newer state.
  useEffect(() => {
    let cancelled = false

    async function load() {
      const pages: FilterPresetPage[] = [
        'dashboard',
        ...(canViewReports ? (['reports'] as FilterPresetPage[]) : []),
      ]
      // One failed fetch must not discard the other page's presets.
      const results = await Promise.allSettled(
        pages.map((page) => fetchFilterPresets(page)),
      )
      if (cancelled) return
      const next: PresetEntry[] = []
      results.forEach((result, index) => {
        // Guard against malformed responses — one bad page must not break
        // the sidebar.
        if (result.status !== 'fulfilled' || !Array.isArray(result.value)) return
        const page = pages[index]
        for (const preset of result.value) {
          const qs = filtersToQueryString(preset.filters)
          next.push({ preset, to: `${PAGE_ROUTES[page]}${qs ? `?${qs}` : ''}` })
        }
      })
      setEntries(next)
    }

    void load()
    // Refresh when any filter-bar menu creates/updates/renames/deletes a
    // preset — the sidebar stays mounted, so mount-only fetching goes stale.
    const handler = () => void load()
    window.addEventListener(PRESETS_CHANGED_EVENT, handler)
    return () => {
      cancelled = true
      window.removeEventListener(PRESETS_CHANGED_EVENT, handler)
    }
  }, [canViewReports])

  const { mine, server } = useMemo(() => {
    const lists = { mine: [] as PresetEntry[], server: [] as PresetEntry[] }
    for (const entry of entries ?? []) {
      (entry.preset.scope === 'server' ? lists.server : lists.mine).push(entry)
    }
    return lists
  }, [entries])

  if (entries === null || entries.length === 0) return null

  const renderItem = (entry: PresetEntry) => {
    const { preset } = entry
    const Icon = preset.scope === 'server' ? Building2 : Star
    const label = preset.name
    const link = (
      <button
        type="button"
        data-testid={`sidebar-filter-preset-${preset.id}`}
        onClick={() => {
          // Tell the target page's presets menu which preset is now active.
          notifyPresetApplied(preset.id, preset.page)
          navigate(entry.to)
          onNavigate?.()
        }}
        className={cn(
          'flex w-full items-center gap-2 rounded-md px-3 py-1.5 text-left text-sm text-text-secondary transition-colors hover:bg-surface-hover hover:text-text-primary',
          collapsed && 'justify-center px-0 py-2',
        )}
      >
        <Icon className="h-3.5 w-3.5 shrink-0 text-text-tertiary" />
        {!collapsed && <span className="truncate">{label}</span>}
      </button>
    )
    if (collapsed) {
      return (
        <Tooltip key={preset.id}>
          <TooltipTrigger asChild>{link}</TooltipTrigger>
          <TooltipContent side="right">{label}</TooltipContent>
        </Tooltip>
      )
    }
    return <div key={preset.id}>{link}</div>
  }

  const renderGroup = (title: string, list: PresetEntry[]) =>
    list.length > 0 && (
      <>
        {!collapsed && (
          <div className="px-3 pb-1 pt-1.5 text-[10px] font-semibold uppercase tracking-wide text-text-tertiary">
            {title}
          </div>
        )}
        {list.map(renderItem)}
      </>
    )

  return (
    <div data-testid="sidebar-saved-filters" className="pt-1">
      {!collapsed && (
        <div className="px-3 pb-1 pt-2 text-[10px] font-semibold uppercase tracking-wide text-text-tertiary">
          Saved filters
        </div>
      )}
      {renderGroup('Yours', mine)}
      {renderGroup('Server', server)}
    </div>
  )
}
