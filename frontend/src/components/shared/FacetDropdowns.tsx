import { SearchableMultiSelect } from '@/components/ui/searchable-multi-select'
import {
  applyFacetOptionChange,
  isFacetOptionHidden,
  selectedOptionLabelsForFacet,
} from '@/lib/tagFacets'
import type { FilterFacet } from '@/types'

export interface FacetDropdownsProps {
  facets: FilterFacet[]
  selectedLabels: string[]
  onLabelsChange: (nextLabels: string[]) => void
}

/**
 * Renders admin-defined tag facets as searchable multi-select dropdowns inside
 * the shared JobFilterBar (#658). Selections read and write the flat `label=`
 * list so bookmarks, presets, and "Clear filters" work without new URL keys.
 */
export function FacetDropdowns({ facets, selectedLabels, onLabelsChange }: FacetDropdownsProps) {
  if (facets.length === 0) return null

  return (
    <>
      {facets.map((facet) => {
        const selectedOptions = selectedOptionLabelsForFacet(facet, selectedLabels)
        const selectedSet = new Set(selectedOptions)
        const visibleOptions = facet.options.filter(
          (opt) => !isFacetOptionHidden(opt, selectedSet.has(opt.label)),
        )
        if (visibleOptions.length === 0) return null

        return (
          <SearchableMultiSelect
            key={facet.id}
            label={facet.name}
            placeholder={facet.name}
            values={selectedOptions}
            options={visibleOptions.map((opt) => ({
              value: opt.label,
              label: `${opt.label} (${opt.count})`,
            }))}
            onChange={(nextOptions) =>
              onLabelsChange(applyFacetOptionChange(facet, selectedLabels, nextOptions))
            }
            className="w-full sm:w-44"
          />
        )
      })}
    </>
  )
}
