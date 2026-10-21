import { api } from '@/lib/api'

// ─── Types ──────────────────────────────────────────────────────────

export type FilterPresetPage = 'dashboard' | 'reports'
export type FilterPresetScope = 'user' | 'server'

export interface FilterPreset {
  id: number
  name: string
  page: FilterPresetPage
  scope: FilterPresetScope
  filters: Record<string, string | string[]>
  created_at: string
  updated_at: string
}

export type FilterValues = Record<string, string | string[]>

// URL query params each page's presets may carry (#179) — mirrors the
// backend _FILTER_PRESET_KEYS allowlist.
export const FILTER_PRESET_KEYS: Record<FilterPresetPage, readonly string[]> = {
  dashboard: [
    'team', 'tier', 'version', 'label', 'exclude_label',
    'status', 'review_status', 'analysis_state', 'date_from', 'date_to',
  ],
  reports: [
    'team', 'tier', 'version', 'label', 'exclude_label',
    'status', 'review_status', 'from', 'to',
  ],
}

// ─── Cross-component notifications ─────────────────────────────────
// The sidebar lists presets read-only while the filter-bar menus mutate
// them; these window events keep every view in sync without global state.

export const PRESETS_CHANGED_EVENT = 'rootcoz:presets-changed'
export const PRESET_APPLIED_EVENT = 'rootcoz:preset-applied'

/** Broadcast that the preset list changed (created/updated/renamed/deleted). */
export function notifyPresetsChanged(): void {
  window.dispatchEvent(new CustomEvent(PRESETS_CHANGED_EVENT))
}

let lastAppliedPreset: { id: number; page: FilterPresetPage } | null = null

/** Record + broadcast a preset application (e.g. sidebar click), so the
 * target page's menu can mark it active even though it mounts afterwards. */
export function notifyPresetApplied(id: number, page: FilterPresetPage): void {
  lastAppliedPreset = { id, page }
  window.dispatchEvent(new CustomEvent(PRESET_APPLIED_EVENT, { detail: lastAppliedPreset }))
}

/** The most recently applied preset (survives page navigation). */
export function getLastAppliedPreset(): { id: number; page: FilterPresetPage } | null {
  return lastAppliedPreset
}

/** Reset the applied-preset tracking (test isolation only). */
export function resetLastAppliedPresetForTests(): void {
  lastAppliedPreset = null
}

// ─── API ────────────────────────────────────────────────────────────

export async function fetchFilterPresets(page: FilterPresetPage): Promise<FilterPreset[]> {
  return api.get<FilterPreset[]>(`/api/user/filter-presets?page=${page}`)
}

export async function createFilterPreset(
  name: string,
  page: FilterPresetPage,
  scope: FilterPresetScope,
  filters: FilterValues,
): Promise<FilterPreset> {
  return api.post<FilterPreset>('/api/user/filter-presets', { name, page, scope, filters })
}

export async function updateFilterPreset(
  id: number,
  body: { name?: string; filters?: FilterValues },
): Promise<FilterPreset> {
  return api.put<FilterPreset>(`/api/user/filter-presets/${id}`, body)
}

export async function deleteFilterPreset(id: number): Promise<void> {
  await api.delete(`/api/user/filter-presets/${id}`)
}

// ─── URL param conversion ───────────────────────────────────────────

/** Extract allowlisted filter values from URL search params. */
export function searchParamsToFilters(
  params: URLSearchParams,
  keys: readonly string[],
): FilterValues {
  const out: FilterValues = {}
  for (const key of keys) {
    const all = params.getAll(key).filter((v) => v !== '')
    if (all.length === 0) continue
    out[key] = all.length > 1 ? all : all[0]
  }
  return out
}

/** Replace allowlisted filter params in a URLSearchParams copy, leaving
 * everything else (e.g. `search`) untouched. */
export function applyFiltersToParams(
  params: URLSearchParams,
  filters: FilterValues,
  keys: readonly string[],
): URLSearchParams {
  const next = new URLSearchParams(params)
  for (const key of keys) next.delete(key)
  for (const [key, value] of Object.entries(filters)) {
    if (!keys.includes(key)) continue
    for (const v of Array.isArray(value) ? value : [value]) next.append(key, v)
  }
  return next
}

/** Build a query string from filter values (sidebar navigation). */
export function filtersToQueryString(filters: FilterValues): string {
  const params = new URLSearchParams()
  for (const [key, value] of Object.entries(filters)) {
    for (const v of Array.isArray(value) ? value : [value]) params.append(key, v)
  }
  return params.toString()
}

// ─── Comparison (modified detection) ────────────────────────────────

const asSortedArray = (value: string | string[]): string[] =>
  (Array.isArray(value) ? [...value] : [value]).sort()

/** Deep-compare two filter value maps (order-insensitive for lists). */
export function filtersEqual(a: FilterValues, b: FilterValues): boolean {
  const ka = Object.keys(a).sort()
  const kb = Object.keys(b).sort()
  if (ka.length !== kb.length || ka.some((k, i) => k !== kb[i])) return false
  return ka.every((k) => {
    const va = asSortedArray(a[k])
    const vb = asSortedArray(b[k])
    return va.length === vb.length && va.every((v, i) => v === vb[i])
  })
}
