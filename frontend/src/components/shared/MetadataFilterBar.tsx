import { useCallback, useEffect, useState } from 'react'
import { api } from '@/lib/api'
import type { JobMetadata } from '@/types'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { Button } from '@/components/ui/button'
import { X } from 'lucide-react'

const ALL_VALUE = '__ALL__'
const OPTION_PREFIX = 'value:'

function encodeSelectValue(value: string): string {
  return `${OPTION_PREFIX}${encodeURIComponent(value)}`
}

function decodeSelectValue(value: string): string {
  if (value === ALL_VALUE) return ''
  return decodeURIComponent(value.slice(OPTION_PREFIX.length))
}

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

// ─── Select-based dropdowns (team / tier / version) ────────────────────────

interface MetadataDropdownsProps {
  options: MetadataOptions
  team: string
  tier: string
  version: string
  onTeamChange: (value: string) => void
  onTierChange: (value: string) => void
  onVersionChange: (value: string) => void
}

const SELECT_FILTERS_CONFIG = [
  { key: 'team', allLabel: 'All teams', aria: 'Filter by team' },
  { key: 'tier', allLabel: 'All tiers', aria: 'Filter by tier' },
  { key: 'version', allLabel: 'All versions', aria: 'Filter by version' },
] as const

function buildOptionItems(options: string[], activeValue: string): string[] {
  if (!activeValue || options.includes(activeValue)) return options
  return [activeValue, ...options]
}

/** Renders team/tier/version select dropdowns. Renders nothing if no options exist. */
export function MetadataDropdowns({
  options,
  team,
  tier,
  version,
  onTeamChange,
  onTierChange,
  onVersionChange,
}: MetadataDropdownsProps) {
  const values: Record<string, string> = { team, tier, version }
  const optionsMap: Record<string, string[]> = { team: options.teams, tier: options.tiers, version: options.versions }
  const handlers: Record<string, (v: string) => void> = { team: onTeamChange, tier: onTierChange, version: onVersionChange }

  return (
    <>
      {SELECT_FILTERS_CONFIG
        .filter((f) => optionsMap[f.key].length > 0 || !!values[f.key])
        .map((f) => {
          const items = buildOptionItems(optionsMap[f.key], values[f.key])
          return (
            <Select
              key={f.key}
              value={values[f.key] ? encodeSelectValue(values[f.key]) : ALL_VALUE}
              onValueChange={(v) => handlers[f.key](decodeSelectValue(v))}
            >
              <SelectTrigger aria-label={f.aria} className="w-full sm:w-32">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={ALL_VALUE}>{f.allLabel}</SelectItem>
                {items.map((option) => (
                  <SelectItem key={option} value={encodeSelectValue(option)}>{option}</SelectItem>
                ))}
              </SelectContent>
            </Select>
          )
        })}
    </>
  )
}

// ─── Label chip buttons ────────────────────────────────────────────────────

interface MetadataLabelChipsProps {
  allLabels: string[]
  labels: string[]
  onLabelsChange: (value: string[]) => void
}

/** Renders a row of toggle-able label chips. Renders nothing if no labels exist. */
export function MetadataLabelChips({ allLabels, labels, onLabelsChange }: MetadataLabelChipsProps) {
  const toggleLabel = useCallback((label: string) => {
    if (labels.includes(label)) {
      onLabelsChange(labels.filter((l) => l !== label))
    } else {
      onLabelsChange([...labels, label])
    }
  }, [labels, onLabelsChange])

  if (allLabels.length === 0 && labels.length === 0) return null

  const displayLabels = allLabels.length > 0
    ? [...new Set([...allLabels, ...labels])].sort()
    : labels

  return (
    <div className="flex flex-wrap items-center gap-2">
      <span className="text-xs text-text-tertiary">Filter by tag:</span>
      {displayLabels.map((label) => (
        <button
          type="button"
          key={label}
          aria-pressed={labels.includes(label)}
          className={`cursor-pointer text-xs px-2 py-0.5 rounded-md border transition-colors ${
            labels.includes(label)
              ? 'bg-signal-green/20 text-signal-green border-signal-green/40 hover:bg-signal-green/30'
              : 'border-border-muted text-text-tertiary hover:bg-surface-hover hover:text-text-secondary'
          }`}
          onClick={() => toggleLabel(label)}
        >
          {label}
        </button>
      ))}
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
