import { useEffect, useState } from 'react'
import { api } from '@/lib/api'
import type { JobMetadata } from '@/types'
import { Button } from '@/components/ui/button'
import { Tooltip, TooltipTrigger, TooltipContent } from '@/components/ui/tooltip'
import { X } from 'lucide-react'
import { MultiSelectFilter } from '@/components/shared/MultiSelectFilter'

export interface MetadataOptions {
  teams: string[]
  tiers: string[]
  versions: string[]
  allLabels: string[]
}

const EMPTY_OPTIONS: MetadataOptions = { teams: [], tiers: [], versions: [], allLabels: [] }

/** Fetches distinct metadata values from the API. */
export function useMetadataOptions(): { options: MetadataOptions; loadError: boolean } {
  const [options, setOptions] = useState<MetadataOptions>(EMPTY_OPTIONS)
  const [loadError, setLoadError] = useState(false)

  useEffect(() => {
    let cancelled = false
    api.get<JobMetadata[]>('/api/jobs/metadata').then((data) => {
      if (cancelled) return
      setLoadError(false)
      const teams = new Set<string>()
      const tiers = new Set<string>()
      const versions = new Set<string>()
      const allLabels = new Set<string>()
      for (const m of data) {
        if (m.team) teams.add(m.team)
        if (m.tier != null) tiers.add(String(m.tier))
        if (m.version) versions.add(m.version)
        for (const l of m.labels) allLabels.add(l)
      }
      setOptions({
        teams: [...teams].sort(),
        tiers: [...tiers].sort((a, b) => {
          const na = Number(a), nb = Number(b)
          if (!isNaN(na) && !isNaN(nb)) return na - nb
          if (!isNaN(na)) return -1
          if (!isNaN(nb)) return 1
          return a.localeCompare(b)
        }),
        versions: [...versions].sort(),
        allLabels: [...allLabels].sort(),
      })
    }).catch(() => {
      if (!cancelled) setLoadError(true)
    })
    return () => { cancelled = true }
  }, [])

  return { options, loadError }
}

// ─── Multi-select dropdowns (team / tier / version) ────────────────────────

interface MetadataDropdownsProps {
  options: MetadataOptions
  teams: Set<string>
  tiers: Set<string>
  versions: Set<string>
  onTeamToggle: (value: string) => void
  onTierToggle: (value: string) => void
  onVersionToggle: (value: string) => void
  onTeamClear: () => void
  onTierClear: () => void
  onVersionClear: () => void
}

const SELECT_FILTERS_CONFIG = [
  { key: 'team', allLabel: 'All teams' },
  { key: 'tier', allLabel: 'All tiers' },
  { key: 'version', allLabel: 'All versions' },
] as const

/** Renders team/tier/version multi-select dropdowns. Renders nothing if no options exist. */
export function MetadataDropdowns({
  options,
  teams,
  tiers,
  versions,
  onTeamToggle,
  onTierToggle,
  onVersionToggle,
  onTeamClear,
  onTierClear,
  onVersionClear,
}: MetadataDropdownsProps) {
  const selectedMap: Record<string, Set<string>> = { team: teams, tier: tiers, version: versions }
  const optionsMap: Record<string, string[]> = { team: options.teams, tier: options.tiers, version: options.versions }
  const toggleHandlers: Record<string, (v: string) => void> = { team: onTeamToggle, tier: onTierToggle, version: onVersionToggle }
  const clearHandlers: Record<string, () => void> = { team: onTeamClear, tier: onTierClear, version: onVersionClear }

  return (
    <>
      {SELECT_FILTERS_CONFIG
        .filter((f) => optionsMap[f.key].length > 0 || selectedMap[f.key].size > 0)
        .map((f) => (
          <MultiSelectFilter
            key={f.key}
            label={f.allLabel}
            options={optionsMap[f.key]}
            selected={selectedMap[f.key]}
            onToggle={toggleHandlers[f.key]}
            onClear={clearHandlers[f.key]}
            className="w-full sm:w-32"
          />
        ))}
    </>
  )
}

// ─── Label chip buttons ────────────────────────────────────────────────────

interface MetadataLabelChipsProps {
  allLabels: string[]
  labels: string[]
  excludeLabels: string[]
  onLabelToggle: (label: string, action: 'include' | 'exclude' | 'off') => void
}

/** Renders a row of toggle-able label chips. Left-click toggles include/off. Right-click toggles exclude. */
export function MetadataLabelChips({ allLabels, labels, excludeLabels, onLabelToggle }: MetadataLabelChipsProps) {
  if (allLabels.length === 0 && labels.length === 0 && excludeLabels.length === 0) return null

  const displayLabels = allLabels.length > 0
    ? [...new Set([...allLabels, ...labels, ...excludeLabels])].sort()
    : [...new Set([...labels, ...excludeLabels])].sort()

  return (
    <div className="flex flex-wrap items-center gap-1.5">
      <span className="text-xs text-text-tertiary">Tags:</span>
      {displayLabels.map((label) => {
        const isIncluded = labels.includes(label)
        const isExcluded = excludeLabels.includes(label)
        const tooltipText = isIncluded
          ? 'Click to remove filter • Right-click to exclude'
          : isExcluded
            ? 'Click to remove exclusion'
            : 'Click to filter by this tag • Right-click to exclude'
        return (
          <Tooltip key={label}>
            <TooltipTrigger asChild>
              <button
                type="button"
                className={`inline-flex items-center rounded-full px-2.5 py-0.5 text-[10px] font-medium cursor-pointer transition-colors ${
                  isIncluded
                    ? 'bg-signal-blue/10 text-signal-blue'
                    : isExcluded
                      ? 'bg-signal-red/10 text-signal-red line-through'
                      : 'bg-surface-elevated text-text-tertiary hover:bg-surface-hover hover:text-text-secondary'
                }`}
                onClick={() => {
                  if (isIncluded) onLabelToggle(label, 'off')
                  else if (isExcluded) onLabelToggle(label, 'off')
                  else onLabelToggle(label, 'include')
                }}
                onContextMenu={(e) => {
                  e.preventDefault()
                  if (isExcluded) onLabelToggle(label, 'off')
                  else onLabelToggle(label, 'exclude')
                }}
              >
                {isExcluded ? `× ${label}` : label}
              </button>
            </TooltipTrigger>
            <TooltipContent>{tooltipText}</TooltipContent>
          </Tooltip>
        )
      })}
    </div>
  )
}

// ─── Combined clear-filters button ─────────────────────────────────────────

interface MetadataClearButtonProps {
  hasFilters: boolean
  onClearAll: () => void
}

/** Renders a "Clear metadata" button when metadata filters are active. */
export function MetadataClearButton({ hasFilters, onClearAll }: MetadataClearButtonProps) {
  if (!hasFilters) return null
  return (
    <Button variant="ghost" size="sm" onClick={onClearAll} className="h-7 px-2 text-xs text-text-tertiary hover:text-text-secondary">
      <X className="h-3 w-3 mr-1" />
      Clear metadata
    </Button>
  )
}
