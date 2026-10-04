import { useEffect, useState } from 'react'
import { api } from '@/lib/api'
import type { JobMetadata } from '@/types'

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
