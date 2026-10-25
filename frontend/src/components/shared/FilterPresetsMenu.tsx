import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Button } from '@/components/ui/button'
import { ConfirmDialog } from '@/components/shared/ConfirmDialog'
import {
  Building2,
  Check,
  ChevronDown,
  Pencil,
  Plus,
  Star,
  Trash2,
  X,
} from 'lucide-react'
import { extractApiDetail } from '@/lib/api'
import { useAuth } from '@/lib/auth'
import { cn } from '@/lib/utils'
import {
  createFilterPreset,
  deleteFilterPreset,
  fetchFilterPresets,
  filtersEqual,
  FILTER_PRESET_KEYS,
  getLastAppliedPreset,
  notifyPresetApplied,
  notifyPresetsChanged,
  PRESET_APPLIED_EVENT,
  searchParamsToFilters,
  updateFilterPreset,
  type FilterPreset,
  type FilterPresetPage,
  type FilterPresetScope,
  type FilterValues,
} from '@/lib/filterPresets'

// ─── Component ──────────────────────────────────────────────────────

interface FilterPresetsMenuProps {
  /** Page whose presets this menu manages. */
  page: FilterPresetPage
  /** Current URL search params — the source of the filters being saved. */
  searchParams: URLSearchParams
  /** Apply a preset's filters (the page writes them into the URL). */
  onApply: (filters: FilterValues) => void
}

/**
 * Filter-bar presets menu (#179): save the current filter combination as a
 * named preset (personal or, for operators, server-wide), load/rename/delete
 * presets, and track whether the loaded preset has been modified.
 */
export function FilterPresetsMenu({ page, searchParams, onApply }: FilterPresetsMenuProps) {
  const { isOperator } = useAuth()
  const keys = FILTER_PRESET_KEYS[page]

  const [presets, setPresets] = useState<FilterPreset[] | null>(null)
  const [activeId, setActiveId] = useState<number | null>(null)
  const [loadError, setLoadError] = useState(false)

  const [saveOpen, setSaveOpen] = useState(false)
  const [saveName, setSaveName] = useState('')
  const [saveScope, setSaveScope] = useState<FilterPresetScope>('user')
  const [saveError, setSaveError] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)

  const [renameTarget, setRenameTarget] = useState<FilterPreset | null>(null)
  const [renameName, setRenameName] = useState('')
  const [renameError, setRenameError] = useState<string | null>(null)
  const [renaming, setRenaming] = useState(false)

  const [deleteTarget, setDeleteTarget] = useState<FilterPreset | null>(null)
  const [deleting, setDeleting] = useState(false)
  const [deleteError, setDeleteError] = useState<string | null>(null)

  const [busy, setBusy] = useState(false)
  const [actionError, setActionError] = useState<string | null>(null)

  const refresh = useCallback(async () => {
    try {
      const list = await fetchFilterPresets(page)
      setPresets(list)
      setLoadError(false)
      setActiveId((current) => (list.some((p) => p.id === current) ? current : null))
    } catch {
      setLoadError(true)
    }
  }, [page])

  useEffect(() => {
    refresh()
  }, [refresh])

  // Follow externally applied presets (sidebar clicks, cross-page loads):
  // the active preset must track the URL's target instead of staying stuck
  // on the previous selection, or Update would overwrite the wrong preset.
  useEffect(() => {
    const handler = (event: Event) => {
      const applied = (event as CustomEvent<{ id: number; page: FilterPresetPage }>).detail
      if (applied && applied.page === page) setActiveId(applied.id)
    }
    window.addEventListener(PRESET_APPLIED_EVENT, handler)
    return () => window.removeEventListener(PRESET_APPLIED_EVENT, handler)
  }, [page])

  const currentFilters = useMemo(
    () => searchParamsToFilters(searchParams, keys),
    [searchParams, keys],
  )
  const hasFilters = Object.keys(currentFilters).length > 0

  // A clean URL means no preset is selected: returning to the page via a
  // nav link (or clearing every filter) empties the URL filters, and the
  // selection must drop in the same commit as the navigation — adjusting
  // state during render keeps it in the same commit (Layout's pattern).
  const [prevHasFilters, setPrevHasFilters] = useState(hasFilters)
  if (prevHasFilters !== hasFilters) {
    setPrevHasFilters(hasFilters)
    if (!hasFilters) setActiveId(null)
  }

  // On mount, restore the last applied preset only when the URL still
  // carries its filters — a nav-link return leaves a clean URL, so no
  // preset is selected then. Runs once, after the preset list loads.
  const restoredRef = useRef(false)
  useEffect(() => {
    if (restoredRef.current || presets === null) return
    restoredRef.current = true
    const last = getLastAppliedPreset()
    if (!last || last.page !== page) return
    const candidate = presets.find((p) => p.id === last.id)
    if (candidate && filtersEqual(currentFilters, candidate.filters)) {
      setActiveId(candidate.id)
    }
  }, [presets, page, currentFilters])

  const activePreset = presets?.find((p) => p.id === activeId) ?? null
  const modified = activePreset !== null && !filtersEqual(currentFilters, activePreset.filters)

  // ─── Actions ───────────────────────────────────────────────────

  const applyPreset = (preset: FilterPreset) => {
    notifyPresetApplied(preset.id, preset.page)
    setActiveId(preset.id)
    onApply(preset.filters)
  }

  const openSave = () => {
    setSaveName('')
    setSaveScope('user')
    setSaveError(null)
    setSaveOpen(true)
  }

  const save = async () => {
    const name = saveName.trim()
    if (!name) return
    setSaving(true)
    setSaveError(null)
    try {
      const preset = await createFilterPreset(name, page, saveScope, currentFilters)
      setSaveOpen(false)
      setActiveId(preset.id)
      await refresh()
      notifyPresetsChanged()
    } catch (err) {
      setSaveError(extractApiDetail(err) ?? 'Failed to save preset')
    } finally {
      setSaving(false)
    }
  }

  const updateActive = async () => {
    if (!activePreset) return
    setBusy(true)
    setActionError(null)
    try {
      const preset = await updateFilterPreset(activePreset.id, { filters: currentFilters })
      setActiveId(preset.id)
      await refresh()
      notifyPresetsChanged()
    } catch (err) {
      setActionError(extractApiDetail(err) ?? 'Failed to update preset')
    } finally {
      setBusy(false)
    }
  }

  const openRename = (preset: FilterPreset) => {
    setRenameTarget(preset)
    setRenameName(preset.name)
    setRenameError(null)
  }

  const rename = async () => {
    if (!renameTarget) return
    const name = renameName.trim()
    if (!name) return
    setRenaming(true)
    setRenameError(null)
    try {
      await updateFilterPreset(renameTarget.id, { name })
      setRenameTarget(null)
      await refresh()
      notifyPresetsChanged()
    } catch (err) {
      setRenameError(extractApiDetail(err) ?? 'Failed to rename preset')
    } finally {
      setRenaming(false)
    }
  }

  const remove = async () => {
    if (!deleteTarget) return
    setDeleting(true)
    setDeleteError(null)
    try {
      await deleteFilterPreset(deleteTarget.id)
      setDeleteTarget(null)
      await refresh()
      notifyPresetsChanged()
    } catch (err) {
      setDeleteError(extractApiDetail(err) ?? 'Failed to delete preset')
    } finally {
      setDeleting(false)
    }
  }

  const canManage = (preset: FilterPreset) => preset.scope === 'user' || isOperator

  const presetFilterCount = (preset: FilterPreset) => Object.keys(preset.filters).length

  // ─── Render ────────────────────────────────────────────────────

  return (
    <div className="flex items-center gap-1.5" data-testid="filter-presets-menu">
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <button
            type="button"
            aria-label="Saved filter presets"
            data-testid="filter-presets-trigger"
            className={cn(
              'flex h-9 items-center gap-2 rounded-full border px-4 py-2 text-sm transition-colors focus:outline-none focus:ring-2 focus:ring-border-accent focus:ring-offset-1 disabled:cursor-not-allowed disabled:opacity-50',
              activePreset
                ? 'border-signal-blue/40 bg-signal-blue/10 text-text-primary'
                : 'border-border-default bg-surface-elevated text-text-primary hover:bg-surface-hover',
            )}
          >
            {modified && (
              <span
                data-testid="filter-preset-modified-dot"
                title="Filters differ from the loaded preset"
                className="h-2 w-2 shrink-0 rounded-full bg-amber-400"
              />
            )}
            {activePreset ? (
              <Star className="h-3.5 w-3.5 shrink-0 text-signal-blue" fill="currentColor" />
            ) : (
              <Star className="h-3.5 w-3.5 shrink-0 text-text-tertiary" />
            )}
            <span className="max-w-44 truncate">{activePreset ? activePreset.name : 'Presets'}</span>
            <ChevronDown className="h-3.5 w-3.5 opacity-40" />
          </button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="start" className="w-72">
          <DropdownMenuLabel>Saved presets — {page}</DropdownMenuLabel>
          {loadError && (
            <div className="px-3 py-2 text-xs text-text-tertiary">Failed to load presets</div>
          )}
          {presets !== null && presets.length === 0 && !loadError && (
            <div className="px-3 py-2 text-xs text-text-tertiary">
              No presets yet — set filters and save them.
            </div>
          )}
          {presets?.map((preset) => (
            <DropdownMenuItem
              key={preset.id}
              data-testid={`filter-preset-${preset.id}`}
              className="group"
              onSelect={() => applyPreset(preset)}
            >
              {preset.id === activeId ? (
                <Check className="h-3.5 w-3.5 shrink-0 text-signal-blue" />
              ) : (
                <span className="h-3.5 w-3.5 shrink-0" />
              )}
              {preset.scope === 'server' ? (
                <Building2 className="h-3.5 w-3.5 shrink-0 text-text-tertiary" />
              ) : (
                <Star className="h-3.5 w-3.5 shrink-0 text-text-tertiary" />
              )}
              <span className="min-w-0 flex-1 truncate">{preset.name}</span>
              <span className="flex shrink-0 text-xs text-text-tertiary group-hover:hidden group-focus-within:hidden [@media(hover:none)]:hidden">
                {presetFilterCount(preset)} filter{presetFilterCount(preset) === 1 ? '' : 's'}
              </span>
              {canManage(preset) && (
                <span className="hidden shrink-0 items-center gap-1 group-hover:flex group-focus-within:flex [@media(hover:none)]:flex">
                  <button
                    type="button"
                    aria-label={`Rename ${preset.name}`}
                    className="rounded p-1 text-text-tertiary hover:bg-surface-hover hover:text-text-primary"
                    onClick={(e) => {
                      e.preventDefault()
                      e.stopPropagation()
                      openRename(preset)
                    }}
                  >
                    <Pencil className="h-3 w-3" />
                  </button>
                  <button
                    type="button"
                    aria-label={`Delete ${preset.name}`}
                    className="rounded p-1 text-text-tertiary hover:bg-surface-hover hover:text-signal-red"
                    onClick={(e) => {
                      e.preventDefault()
                      e.stopPropagation()
                      setDeleteTarget(preset)
                    }}
                  >
                    <Trash2 className="h-3 w-3" />
                  </button>
                </span>
              )}
            </DropdownMenuItem>
          ))}
          <DropdownMenuSeparator />
          <DropdownMenuItem
            data-testid="filter-preset-save-as"
            disabled={!hasFilters}
            className="text-text-primary"
            onSelect={() => openSave()}
          >
            <Plus className="h-3.5 w-3.5 shrink-0" />
            Save current filters as…
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>

      {modified && activePreset && canManage(activePreset) && (
        <Button
          type="button"
          variant="secondary"
          size="sm"
          data-testid="filter-preset-update"
          className="h-7 rounded-full px-3 text-xs"
          disabled={busy}
          onClick={updateActive}
          title={`Overwrite "${activePreset.name}" with the current filters`}
        >
          Update
        </Button>
      )}

      {actionError && (
        <span
          role="alert"
          data-testid="filter-preset-action-error"
          className="max-w-48 truncate text-xs text-signal-red"
          title={actionError}
        >
          {actionError}
        </span>
      )}

      {activePreset && (
        <button
          type="button"
          aria-label="Unload preset (keeps current filters)"
          title="Unload preset (keeps current filters)"
          data-testid="filter-preset-unload"
          className="rounded-full p-1 text-text-tertiary hover:bg-surface-hover hover:text-text-primary"
          onClick={() => setActiveId(null)}
        >
          <X className="h-3.5 w-3.5" />
        </button>
      )}

      {/* ── Save dialog ── */}
      <Dialog open={saveOpen} onOpenChange={setSaveOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Save current filters</DialogTitle>
            <DialogDescription>
              Give this filter combination a name so you can reuse it later.
            </DialogDescription>
          </DialogHeader>
          <Input
            value={saveName}
            onChange={(e) => setSaveName(e.target.value)}
            placeholder="Preset name"
            maxLength={100}
            data-testid="filter-preset-save-name"
            autoFocus
          />
          {saveError && (
            <p role="alert" data-testid="filter-preset-save-error" className="text-sm text-signal-red">
              {saveError}
            </p>
          )}
          <div className="flex gap-2">
            <Button
              type="button"
              variant={saveScope === 'user' ? 'default' : 'secondary'}
              size="sm"
              className="flex-1"
              onClick={() => setSaveScope('user')}
            >
              Personal
            </Button>
            <Button
              type="button"
              variant={saveScope === 'server' ? 'default' : 'secondary'}
              size="sm"
              className="flex-1"
              disabled={!isOperator}
              title={isOperator ? 'Visible to every user' : 'Server filters require operator role'}
              onClick={() => setSaveScope('server')}
            >
              Server filter
            </Button>
          </div>
          <p className="text-xs text-text-tertiary">
            {saveScope === 'user' ? 'Only you can see and use this preset.' : 'Every user can see and use this preset.'}
          </p>
          <div className="flex flex-wrap gap-1.5">
            {Object.entries(currentFilters).map(([key, value]) => (
              <span
                key={key}
                className="rounded-md border border-border-default bg-surface-elevated px-2 py-0.5 font-mono text-[11px] text-text-secondary"
              >
                <span className="font-semibold text-text-primary">{key}</span>=
                {Array.isArray(value) ? value.join(',') : value}
              </span>
            ))}
          </div>
          <DialogFooter>
            <Button variant="ghost" onClick={() => setSaveOpen(false)} disabled={saving}>
              Cancel
            </Button>
            <Button
              onClick={save}
              disabled={saving || !saveName.trim()}
              data-testid="filter-preset-save-confirm"
            >
              {saving ? 'Saving…' : 'Save preset'}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* ── Rename dialog ── */}
      <Dialog open={renameTarget !== null} onOpenChange={(open) => !open && setRenameTarget(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Rename preset</DialogTitle>
            <DialogDescription>Rename “{renameTarget?.name}”.</DialogDescription>
          </DialogHeader>
          <Input
            value={renameName}
            onChange={(e) => setRenameName(e.target.value)}
            placeholder="Preset name"
            maxLength={100}
            data-testid="filter-preset-rename-name"
            autoFocus
          />
          {renameError && (
            <p role="alert" data-testid="filter-preset-rename-error" className="text-sm text-signal-red">
              {renameError}
            </p>
          )}
          <DialogFooter>
            <Button variant="ghost" onClick={() => setRenameTarget(null)} disabled={renaming}>
              Cancel
            </Button>
            <Button onClick={rename} disabled={renaming || !renameName.trim()}>
              {renaming ? 'Renaming…' : 'Rename'}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* ── Delete confirm ── */}
      <ConfirmDialog
        open={deleteTarget !== null}
        onOpenChange={(open) => !open && setDeleteTarget(null)}
        title="Delete preset?"
        description={`Delete "${deleteTarget?.name}"? This cannot be undone.`}
        confirmLabel="Delete"
        variant="destructive"
        onConfirm={remove}
        loading={deleting}
        error={deleteError}
      />
    </div>
  )
}
