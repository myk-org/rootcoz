import { useMemo, useState } from 'react'
import { AlertTriangle, ArrowDown, ArrowUp, Plus, Trash2 } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { SearchableMultiSelect } from '@/components/ui/searchable-multi-select'
import { useMetadataOptions } from '@/components/shared/useMetadataOptions'
import {
  MAX_FACET_NAME_LENGTH,
  MAX_FACET_OPTIONS,
  MAX_OPTION_LABEL_LENGTH,
  MAX_OPTION_TAGS,
  MAX_TAG_FACETS,
  slugifyFacetId,
} from '@/lib/tagFacets'
import type { TagFacet, TagFacetOption } from '@/types'

export interface TagFacetsEditorProps {
  facets: TagFacet[]
  onChange: (next: TagFacet[]) => void
  disabled?: boolean
}

function moveItem<T>(items: T[], index: number, delta: -1 | 1): T[] {
  const target = index + delta
  if (target < 0 || target >= items.length) return items
  const next = [...items]
  const [moved] = next.splice(index, 1)
  next.splice(target, 0, moved)
  return next
}

/**
 * Structured inline editor for the `tag_facets` Server Setting (#658).
 *
 * Admins create, edit, delete, and reorder facets and options. Option tags are
 * chosen exclusively from live job metadata labels (never free text). Options
 * referencing tags that have since disappeared show an amber warning naming
 * each missing tag without silently rewriting the stored configuration.
 */
export function TagFacetsEditor({ facets, onChange, disabled = false }: TagFacetsEditorProps) {
  const { options: metadataOptions, loadError: metadataLoadError, loaded: metadataLoaded } =
    useMetadataOptions()
  // Track which facet indices have had their ID manually edited so typing a
  // name on a newly added facet can auto-suggest a slug without clobbering edits.
  const [manualIdIndices, setManualIdIndices] = useState<Set<number>>(() => new Set())

  const liveLabels = metadataOptions.allLabels
  const liveLabelSet = useMemo(() => new Set(liveLabels), [liveLabels])
  // Map each tag to the first facet using it so the picker prevents
  // assigning the same tag to two different facets.
  const tagOwnerByFacetIndex = useMemo(() => {
    const owners = new Map<string, { index: number; name: string }>()
    facets.forEach((f, idx) => {
      for (const opt of f.options) {
        for (const tag of opt.tags) {
          if (!owners.has(tag)) owners.set(tag, { index: idx, name: f.name || f.id || `Facet ${idx + 1}` })
        }
      }
    })
    return owners
  }, [facets])

  const updateFacet = (facetIndex: number, updater: (facet: TagFacet) => TagFacet) => {
    onChange(facets.map((f, i) => (i === facetIndex ? updater(f) : f)))
  }

  const handleAddFacet = () => {
    if (facets.length >= MAX_TAG_FACETS) return
    onChange([
      ...facets,
      {
        id: '',
        name: '',
        options: [{ label: '', tags: [] }],
      },
    ])
  }

  const handleDeleteFacet = (facetIndex: number) => {
    onChange(facets.filter((_, i) => i !== facetIndex))
    setManualIdIndices(new Set())
  }

  const handleMoveFacet = (facetIndex: number, delta: -1 | 1) => {
    onChange(moveItem(facets, facetIndex, delta))
    setManualIdIndices(new Set())
  }

  const handleNameChange = (facetIndex: number, nextName: string) => {
    updateFacet(facetIndex, (facet) => {
      const autoSlug = !manualIdIndices.has(facetIndex) && (!facet.id || facet.id === slugifyFacetId(facet.name))
      return {
        ...facet,
        name: nextName,
        id: autoSlug ? slugifyFacetId(nextName) : facet.id,
      }
    })
  }

  const handleIdChange = (facetIndex: number, nextId: string) => {
    setManualIdIndices((prev) => new Set(prev).add(facetIndex))
    updateFacet(facetIndex, (facet) => ({ ...facet, id: nextId }))
  }

  const handleAddOption = (facetIndex: number) => {
    updateFacet(facetIndex, (facet) => {
      if (facet.options.length >= MAX_FACET_OPTIONS) return facet
      return {
        ...facet,
        options: [...facet.options, { label: '', tags: [] }],
      }
    })
  }

  const handleUpdateOption = (
    facetIndex: number,
    optionIndex: number,
    updater: (opt: TagFacetOption) => TagFacetOption,
  ) => {
    updateFacet(facetIndex, (facet) => ({
      ...facet,
      options: facet.options.map((o, i) => (i === optionIndex ? updater(o) : o)),
    }))
  }

  const handleDeleteOption = (facetIndex: number, optionIndex: number) => {
    updateFacet(facetIndex, (facet) => ({
      ...facet,
      options: facet.options.filter((_, i) => i !== optionIndex),
    }))
  }

  const handleMoveOption = (facetIndex: number, optionIndex: number, delta: -1 | 1) => {
    updateFacet(facetIndex, (facet) => ({
      ...facet,
      options: moveItem(facet.options, optionIndex, delta),
    }))
  }

  return (
    <div data-testid="tag-facets-editor" className="space-y-3">
      {metadataLoadError && (
        <p className="flex items-center gap-1.5 text-xs text-signal-amber">
          <AlertTriangle className="h-3.5 w-3.5 shrink-0" />
          Could not load live job tags — tag choices may be unavailable.
        </p>
      )}

      {facets.length === 0 ? (
        <p className="text-xs italic text-text-tertiary">
          No tag facets configured — jobs are filtered with the flat tag row only.
        </p>
      ) : (
        <div className="space-y-3">
          {facets.map((facet, facetIndex) => (
            <div
              key={`facet-${facetIndex}`}
              data-testid={`tag-facet-card-${facetIndex}`}
              className="rounded-md border border-border-default bg-surface-card p-3 space-y-3"
            >
              <div className="flex flex-wrap items-end gap-2">
                <div className="flex-1 min-w-40 space-y-1">
                  <label
                    htmlFor={`facet-name-${facetIndex}`}
                    className="block text-xs font-medium text-text-secondary"
                  >
                    Facet name
                  </label>
                  <Input
                    id={`facet-name-${facetIndex}`}
                    value={facet.name}
                    maxLength={MAX_FACET_NAME_LENGTH}
                    placeholder="e.g. Platform"
                    disabled={disabled}
                    onChange={(e) => handleNameChange(facetIndex, e.target.value)}
                    className="text-sm"
                  />
                </div>
                <div className="w-48 space-y-1">
                  <label
                    htmlFor={`facet-id-${facetIndex}`}
                    className="block text-xs font-medium text-text-secondary"
                  >
                    ID slug
                  </label>
                  <Input
                    id={`facet-id-${facetIndex}`}
                    value={facet.id}
                    maxLength={32}
                    placeholder="e.g. platform"
                    disabled={disabled}
                    onChange={(e) => handleIdChange(facetIndex, e.target.value)}
                    className="font-mono text-sm"
                  />
                </div>
                <div className="flex items-center gap-1">
                  <Button
                    type="button"
                    size="icon"
                    variant="ghost"
                    aria-label={`Move facet ${facet.name || facetIndex + 1} up`}
                    disabled={disabled || facetIndex === 0}
                    onClick={() => handleMoveFacet(facetIndex, -1)}
                    className="h-9 w-9"
                  >
                    <ArrowUp className="h-3.5 w-3.5" />
                  </Button>
                  <Button
                    type="button"
                    size="icon"
                    variant="ghost"
                    aria-label={`Move facet ${facet.name || facetIndex + 1} down`}
                    disabled={disabled || facetIndex === facets.length - 1}
                    onClick={() => handleMoveFacet(facetIndex, 1)}
                    className="h-9 w-9"
                  >
                    <ArrowDown className="h-3.5 w-3.5" />
                  </Button>
                  <Button
                    type="button"
                    size="icon"
                    variant="ghost"
                    aria-label={`Delete facet ${facet.name || facetIndex + 1}`}
                    disabled={disabled}
                    onClick={() => handleDeleteFacet(facetIndex)}
                    className="h-9 w-9 text-text-tertiary hover:text-signal-red"
                  >
                    <Trash2 className="h-3.5 w-3.5" />
                  </Button>
                </div>
              </div>

              <div className="space-y-2 pl-2 border-l-2 border-border-default">
                <div className="text-xs font-medium text-text-tertiary">Options</div>
                {facet.options.map((opt, optionIndex) => {
                  const missingTags =
                    metadataLoaded && !metadataLoadError
                      ? opt.tags.filter((t) => !liveLabelSet.has(t))
                      : []

                  return (
                    <div key={`opt-${facetIndex}-${optionIndex}`} className="space-y-1">
                      <div className="flex flex-wrap items-center gap-2">
                        <Input
                          aria-label={`Option label ${optionIndex + 1} for ${facet.name || `facet ${facetIndex + 1}`}`}
                          value={opt.label}
                          maxLength={MAX_OPTION_LABEL_LENGTH}
                          placeholder="Option label (e.g. AWS)"
                          disabled={disabled}
                          onChange={(e) =>
                            handleUpdateOption(facetIndex, optionIndex, (prev) => ({
                              ...prev,
                              label: e.target.value,
                            }))
                          }
                          className="w-full sm:w-48 text-sm"
                        />
                        <SearchableMultiSelect
                          label={`tags for ${opt.label || `option ${optionIndex + 1}`}`}
                          placeholder="Pick tags…"
                          values={opt.tags}
                          options={liveLabels.map((tag) => {
                            const owner = tagOwnerByFacetIndex.get(tag)
                            const usedElsewhere = owner !== undefined && owner.index !== facetIndex
                            return {
                              value: tag,
                              disabled: usedElsewhere,
                              hint: usedElsewhere ? `Used in ${owner.name}` : undefined,
                            }
                          })}
                          multiple
                          allowCustom={false}
                          loading={!metadataLoaded}
                          disabled={disabled}
                          onChange={(nextTags) =>
                            handleUpdateOption(facetIndex, optionIndex, (prev) => ({
                              ...prev,
                              tags: nextTags.slice(0, MAX_OPTION_TAGS),
                            }))
                          }
                          className="min-w-0 flex-1 max-w-none"
                        />
                        <div className="flex items-center gap-1">
                          <Button
                            type="button"
                            size="icon"
                            variant="ghost"
                            aria-label={`Move option ${opt.label || optionIndex + 1} up`}
                            disabled={disabled || optionIndex === 0}
                            onClick={() => handleMoveOption(facetIndex, optionIndex, -1)}
                            className="h-9 w-9"
                          >
                            <ArrowUp className="h-3.5 w-3.5" />
                          </Button>
                          <Button
                            type="button"
                            size="icon"
                            variant="ghost"
                            aria-label={`Move option ${opt.label || optionIndex + 1} down`}
                            disabled={disabled || optionIndex === facet.options.length - 1}
                            onClick={() => handleMoveOption(facetIndex, optionIndex, 1)}
                            className="h-9 w-9"
                          >
                            <ArrowDown className="h-3.5 w-3.5" />
                          </Button>
                          <Button
                            type="button"
                            size="icon"
                            variant="ghost"
                            aria-label={`Delete option ${opt.label || optionIndex + 1}`}
                            disabled={disabled || facet.options.length <= 1}
                            onClick={() => handleDeleteOption(facetIndex, optionIndex)}
                            className="h-9 w-9 text-text-tertiary hover:text-signal-red"
                          >
                            <Trash2 className="h-3.5 w-3.5" />
                          </Button>
                        </div>
                      </div>
                      {missingTags.length > 0 && (
                        <p
                          role="alert"
                          className="flex items-center gap-1.5 text-xs text-signal-amber"
                        >
                          <AlertTriangle className="h-3.5 w-3.5 shrink-0" />
                          Missing tag{missingTags.length > 1 ? 's' : ''}:{' '}
                          <span className="font-mono">{missingTags.join(', ')}</span> — no longer
                          present in job metadata. Remove or replace before saving.
                        </p>
                      )}
                    </div>
                  )
                })}
                <Button
                  type="button"
                  size="sm"
                  variant="outline"
                  disabled={disabled || facet.options.length >= MAX_FACET_OPTIONS}
                  onClick={() => handleAddOption(facetIndex)}
                  className="h-8 text-xs"
                >
                  <Plus className="mr-1 h-3 w-3" />
                  Add option
                </Button>
              </div>
            </div>
          ))}
        </div>
      )}

      <div className="flex items-center gap-2">
        <Button
          type="button"
          size="sm"
          variant="outline"
          disabled={disabled || facets.length >= MAX_TAG_FACETS}
          onClick={handleAddFacet}
          className="h-8 text-xs"
        >
          <Plus className="mr-1 h-3 w-3" />
          Add facet
        </Button>
        <span className="text-xs text-text-quaternary">
          {facets.length}/{MAX_TAG_FACETS} facets · up to {MAX_FACET_OPTIONS} options each
        </span>
      </div>
    </div>
  )
}
