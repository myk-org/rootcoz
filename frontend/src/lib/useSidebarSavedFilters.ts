import { useCallback, useEffect, useMemo, useState } from 'react'
import { useAuth } from '@/lib/auth'
import {
  fetchFilterPresets,
  PRESETS_CHANGED_EVENT,
  type FilterPreset,
  type FilterPresetPage,
} from '@/lib/filterPresets'
import { filtersToQueryString } from '@/lib/jobFilters'

export interface PresetEntry {
  preset: FilterPreset
  to: string
}

export const DISCLOSURES = [
  'Dashboard',
  'DashboardServer',
  'DashboardUser',
  'Reports',
  'ReportsServer',
  'ReportsUser',
] as const

export type Disclosure = (typeof DISCLOSURES)[number]
export type Expansion = Record<Disclosure, boolean>

export interface PresetGroups {
  server: PresetEntry[]
  user: PresetEntry[]
}

const PAGE_ROUTES: Record<FilterPresetPage, string> = {
  dashboard: '/',
  reports: '/reports',
}

function storageKey(owner: string, choice: Disclosure): string {
  return `rootcoz_sidebar_filters:${encodeURIComponent(owner)}:${choice}`
}

function createDefaultExpansion(): Expansion {
  return {
    Dashboard: true,
    DashboardServer: true,
    DashboardUser: true,
    Reports: true,
    ReportsServer: true,
    ReportsUser: true,
  }
}

function readExpansion(owner: string | null): Expansion {
  const expanded = createDefaultExpansion()
  if (!owner) return expanded
  try {
    const storage = window.localStorage
    for (const choice of DISCLOSURES) {
      expanded[choice] = storage.getItem(storageKey(owner, choice)) !== 'false'
    }
  } catch {
    console.debug('Sidebar disclosure storage unavailable')
  }
  return expanded
}

interface SidebarSavedFiltersState {
  owner: string | null
  expanded: Expansion
  entries: PresetEntry[]
}

export function useSidebarSavedFilters() {
  const { username, authenticated, loading, canViewReports, isAdmin } = useAuth()
  const owner = authenticated && !loading && username ? username : null
  const reportsAllowed = Boolean(owner) && (canViewReports || isAdmin)

  const [state, setState] = useState<SidebarSavedFiltersState>(() => ({
    owner,
    expanded: readExpansion(owner),
    entries: [],
  }))

  // Synchronously reset state during render on identity change so a previous
  // user's presets or disclosure preferences are never exposed for a frame.
  let currentState = state
  if (currentState.owner !== owner) {
    currentState = { owner, expanded: readExpansion(owner), entries: [] }
    setState(currentState)
  } else if (
    !reportsAllowed &&
    currentState.entries.some((entry) => entry.preset.page === 'reports')
  ) {
    currentState = {
      ...currentState,
      entries: currentState.entries.filter((entry) => entry.preset.page !== 'reports'),
    }
    setState(currentState)
  }

  useEffect(() => {
    if (!owner) return
    let cancelled = false
    let generation = 0

    async function load() {
      const request = ++generation
      const pages: FilterPresetPage[] = reportsAllowed
        ? ['dashboard', 'reports']
        : ['dashboard']
      const results = await Promise.allSettled(
        pages.map((page) => fetchFilterPresets(page)),
      )
      if (cancelled || generation !== request) return

      const entries: PresetEntry[] = []
      results.forEach((result, index) => {
        const page = pages[index]
        if (result.status !== 'fulfilled' || !Array.isArray(result.value)) {
          console.debug('Sidebar preset page load failed:', page)
          return
        }
        for (const preset of result.value) {
          if (!preset || typeof preset !== 'object' || preset.page !== page) continue
          const qs = filtersToQueryString(preset.filters)
          entries.push({ preset, to: `${PAGE_ROUTES[page]}${qs ? `?${qs}` : ''}` })
        }
      })

      setState((current) => (current.owner === owner ? { ...current, entries } : current))
    }

    void load()
    const handlePresetsChanged = () => {
      void load()
    }
    window.addEventListener(PRESETS_CHANGED_EVENT, handlePresetsChanged)
    return () => {
      cancelled = true
      window.removeEventListener(PRESETS_CHANGED_EVENT, handlePresetsChanged)
    }
  }, [owner, reportsAllowed])

  const groups = useMemo(() => {
    const out: Record<FilterPresetPage, PresetGroups> = {
      dashboard: { server: [], user: [] },
      reports: { server: [], user: [] },
    }
    if (!owner || currentState.owner !== owner) return out
    for (const entry of currentState.entries) {
      if (entry.preset.page === 'reports' && !reportsAllowed) continue
      out[entry.preset.page][entry.preset.scope === 'server' ? 'server' : 'user'].push(entry)
    }
    return out
  }, [currentState.owner, currentState.entries, owner, reportsAllowed])

  const toggle = useCallback(
    (choice: Disclosure) => {
      if (!owner || currentState.owner !== owner) return
      const nextValue = !currentState.expanded[choice]
      setState((current) =>
        current.owner === owner
          ? { ...current, expanded: { ...current.expanded, [choice]: nextValue } }
          : current,
      )
      try {
        window.localStorage.setItem(storageKey(owner, choice), String(nextValue))
      } catch {
        console.debug('Sidebar disclosure storage write failed')
      }
    },
    [owner, currentState.owner, currentState.expanded],
  )

  return { groups, expanded: currentState.expanded, toggle }
}
