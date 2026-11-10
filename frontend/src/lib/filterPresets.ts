import { api } from '@/lib/api'
import {
  JOB_FILTER_KEYS,
  TEST_NAME_FILTER_KEYS,
  type FilterValues,
} from './jobFilters'

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

// URL query params each page's presets may carry (#179) — mirrors the
// backend _FILTER_PRESET_KEYS allowlist. Derived from the one canonical list in
// lib/jobFilters.ts (#546), which also owns the helpers below.
export const FILTER_PRESET_KEYS: Record<FilterPresetPage, readonly string[]> = {
  dashboard: [...JOB_FILTER_KEYS, ...TEST_NAME_FILTER_KEYS],
  reports: [...JOB_FILTER_KEYS, ...TEST_NAME_FILTER_KEYS],
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
