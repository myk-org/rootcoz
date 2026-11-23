import type { FilterFacet, FilterFacetOption, TagFacet } from '@/types'

export const MAX_TAG_FACETS = 12
export const MAX_FACET_OPTIONS = 30
export const MAX_OPTION_TAGS = 20
export const MAX_FACET_NAME_LENGTH = 48
export const MAX_OPTION_LABEL_LENGTH = 64
export const FACET_ID_RE = /^[a-z0-9-]{1,32}$/

/** Parse the JSON string stored in the `tag_facets` server setting. */
export function parseTagFacetsJson(raw: string): TagFacet[] {
  const trimmed = raw.trim()
  if (!trimmed) return []
  try {
    const parsed = JSON.parse(trimmed)
    if (!Array.isArray(parsed)) return []
    return parsed
      .filter((item): item is Record<string, unknown> => Boolean(item && typeof item === 'object'))
      .map((item) => ({
        id: typeof item.id === 'string' ? item.id : '',
        name: typeof item.name === 'string' ? item.name : '',
        options: Array.isArray(item.options)
          ? item.options
              .filter((opt): opt is Record<string, unknown> => Boolean(opt && typeof opt === 'object'))
              .map((opt) => ({
                label: typeof opt.label === 'string' ? opt.label : '',
                tags: Array.isArray(opt.tags)
                  ? opt.tags.filter((t): t is string => typeof t === 'string')
                  : [],
              }))
          : [],
      }))
  } catch {
    return []
  }
}

/** Serialize facets into canonical compact JSON matching backend storage. */
export function serializeTagFacets(facets: TagFacet[]): string {
  return JSON.stringify(
    facets.map((f) => ({
      id: f.id.trim(),
      name: f.name.trim(),
      options: f.options.map((o) => ({
        label: o.label.trim(),
        tags: [...new Set(o.tags.map((t) => t.trim()).filter(Boolean))],
      })),
    })),
  )
}

/** Suggest a valid facet slug from a human-readable facet name. */
export function slugifyFacetId(name: string): string {
  return name
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 32)
}

/** Whether a facet option should be hidden (all its tags disappeared and it is not selected). */
export function isFacetOptionHidden(option: FilterFacetOption, selected: boolean): boolean {
  if (selected) return false
  return option.tags.length > 0 && option.missing_tags.length === option.tags.length
}

/** Return the option labels currently active for *facet* given the flat `label=` list. */
export function selectedOptionLabelsForFacet(
  facet: FilterFacet,
  selectedLabels: readonly string[],
): string[] {
  const labelSet = new Set(selectedLabels)
  return facet.options
    .filter((opt) => opt.tags.length > 0 && opt.tags.every((t) => labelSet.has(t)))
    .map((opt) => opt.label)
}

/**
 * Compute the next flat `label=` array when a facet's selected options change.
 *
 * Preserves tags belonging to other facets or ungrouped tag chips; removes tags
 * of deselected options unless another still-selected option in this facet also
 * references them.
 */
export function applyFacetOptionChange(
  facet: FilterFacet,
  selectedLabels: readonly string[],
  nextOptionLabels: readonly string[],
): string[] {
  const prevSelected = new Set(selectedOptionLabelsForFacet(facet, selectedLabels))
  const nextSelected = new Set(nextOptionLabels)

  const stillNeededTags = new Set<string>()
  for (const opt of facet.options) {
    if (nextSelected.has(opt.label)) {
      for (const tag of opt.tags) stillNeededTags.add(tag)
    }
  }

  const tagsToRemove = new Set<string>()
  for (const opt of facet.options) {
    if (prevSelected.has(opt.label) && !nextSelected.has(opt.label)) {
      for (const tag of opt.tags) {
        if (!stillNeededTags.has(tag)) tagsToRemove.add(tag)
      }
    }
  }

  const result: string[] = []
  const seen = new Set<string>()
  for (const label of selectedLabels) {
    if (tagsToRemove.has(label) || seen.has(label)) continue
    seen.add(label)
    result.push(label)
  }

  for (const opt of facet.options) {
    if (!nextSelected.has(opt.label)) continue
    for (const tag of opt.tags) {
      if (!seen.has(tag)) {
        seen.add(tag)
        result.push(tag)
      }
    }
  }

  return result
}
