import { useCallback, useEffect, useRef, useState } from 'react'
import { X } from 'lucide-react'
import { Select, SelectContent, SelectItem, SelectTrigger } from '@/components/ui/select'
import { SearchableMultiSelect, type SearchableMultiSelectOption } from '@/components/ui/searchable-multi-select'
import { api } from '@/lib/api'
import { TEST_NAME_MODES, type TestNameMode } from '@/lib/jobFilters'

/** Quiet period before a typed fragment is looked up. */
const DEBOUNCE_MS = 300
/** Suggestion rows. The server default is 20 and never ships the whole name space. */
const SUGGESTION_LIMIT = 20

interface TestNameSuggestion {
  test_name: string
  failure_count: number
}

interface TestNameFilterProps {
  /** The committed name from the URL, or '' when the filter is off. */
  name: string
  mode: TestNameMode
  /** Called with the name and the mode together, so one never travels alone. */
  onChange: (name: string, mode: TestNameMode) => void
}

const modeLabel = (mode: TestNameMode): string =>
  TEST_NAME_MODES.find((m) => m.value === mode)?.label ?? mode

/**
 * "Every job where this test failed": literal text plus a match mode (#546).
 *
 * The text is literal — a `*` in a test name is a character — so the mode, not
 * the text, says whether the match is exact, a prefix, a suffix or anywhere.
 * The control is the shared `SearchableMultiSelect` (the one "Submitted by"
 * uses), fed by the server's top ~20 **failing** names with their counts: a
 * pick is one exact name, a typed value is the free entry it offers.
 */
export function TestNameFilter({ name, mode, onChange }: TestNameFilterProps) {
  const [suggestions, setSuggestions] = useState<TestNameSuggestion[]>([])
  const [loading, setLoading] = useState(false)
  // A mode picked before there is a name to carry: a mode alone is not a filter,
  // so the URL drops it and the pick waits here for the next name.
  const [pendingMode, setPendingMode] = useState<TestNameMode | null>(null)
  // The pick belongs to the filter state it was made under: a preset, navigation
  // or Clear all changes the props, and the waiting mode goes with them instead
  // of resurfacing on the next name. Adjusting during render is how React
  // resets state on a prop change — an effect here would also trip
  // react-hooks/set-state-in-effect.
  const filterState = `${name}\u0000${mode}`
  const [seenState, setSeenState] = useState(filterState)
  if (seenState !== filterState) {
    setSeenState(filterState)
    if (pendingMode) setPendingMode(null)
  }
  const shownMode = name ? mode : pendingMode ?? mode
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const lookup = useRef(0)

  /** Look the fragment up once typing stops, keeping only the latest answer. */
  const load = useCallback((query: string) => {
    if (timer.current) clearTimeout(timer.current)
    timer.current = setTimeout(() => {
      timer.current = null
      const mine = ++lookup.current
      setLoading(true)
      void api
        .get<{ test_names?: TestNameSuggestion[] }>(
          `/api/dashboard/test-names?q=${encodeURIComponent(query)}&limit=${SUGGESTION_LIMIT}`,
        )
        .catch(() => null)
        .then((data) => {
          // An earlier answer must never replace a later one.
          if (mine !== lookup.current) return
          setLoading(false)
          setSuggestions(data?.test_names ?? [])
        })
    }, DEBOUNCE_MS)
  }, [])

  // The top failing names are worth having before anything is typed.
  useEffect(() => {
    load('')
  }, [load])

  useEffect(
    () => () => {
      if (timer.current) clearTimeout(timer.current)
    },
    [],
  )

  const commit = (value: string) => {
    setPendingMode(null)
    onChange(value, shownMode)
  }

  const options: SearchableMultiSelectOption[] = suggestions.map((row) => ({
    value: row.test_name,
    // The count is the reason the name is worth picking, and never the value.
    hint: String(row.failure_count),
  }))

  return (
    <div data-testid="test-name-filter" className="relative flex w-full items-center gap-2 sm:w-auto">
      <SearchableMultiSelect
        label="Test name"
        multiple={false}
        values={name ? [name] : []}
        options={options}
        loading={loading}
        allowCustom
        // The other modes want the fragment the user typed, not the full name
        // that happens to rank first: free entry leads the list.
        customFirst
        onQueryChange={load}
        onChange={(values) => commit(values[0] ?? '')}
        placeholder="Filter by failing test name"
        className="w-full sm:w-64"
      />
      {name && (
        // One dimension, one value: the shared control shows the pick, and this
        // is how it comes off without clearing every other filter.
        <button
          type="button"
          aria-label="Clear test name"
          onClick={() => commit('')}
          className="shrink-0 rounded text-text-tertiary transition-colors hover:text-signal-red"
        >
          <X className="h-3.5 w-3.5" />
        </button>
      )}
      <Select
        value={shownMode}
        onValueChange={(v) => {
          const next = v as TestNameMode
          setPendingMode(null)
          // A mode with a name travels with it; alone it waits for the next name.
          if (name) onChange(name, next)
          else setPendingMode(next)
        }}
      >
        <SelectTrigger aria-label="Test name match mode" className="w-full sm:w-auto">
          <span className="text-sm">Match: {modeLabel(shownMode)}</span>
        </SelectTrigger>
        <SelectContent>
          {TEST_NAME_MODES.map((option) => (
            <SelectItem key={option.value} value={option.value}>
              {option.label}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
      {name.includes('*') && (
        // Literal text (#546 B.2): a `*` is a character, the mode is how a
        // pattern is expressed. Say so rather than guess at a wildcard.
        <span data-testid="test-name-literal-hint" className="text-xs text-text-tertiary">
          {'“*” is a literal character — pick a match mode instead'}
        </span>
      )}
    </div>
  )
}
