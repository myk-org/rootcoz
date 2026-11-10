import { useEffect, useRef, useState } from 'react'
import { SearchInput } from '@/components/shared/SearchInput'
import { Select, SelectContent, SelectItem, SelectTrigger } from '@/components/ui/select'
import { api } from '@/lib/api'
import { TEST_NAME_MODES, type TestNameMode } from '@/lib/jobFilters'

/** Quiet period before the text is committed and looked up. */
const DEBOUNCE_MS = 300
/** Suggestion rows. The server default is 20 and never ships the whole name space. */
const SUGGESTION_LIMIT = 20

interface TestNameSuggestion {
  test_name: string
  failure_count: number
}

/** The user's edit in flight, and the props it was started against. */
interface Draft {
  text: string
  mode: TestNameMode
  base: { name: string; mode: TestNameMode }
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
 * The box commits and looks up on the debounce, and suggestions are the top
 * ~20 failing names from the server rather than the whole name space.
 */
export function TestNameFilter({ name, mode, onChange }: TestNameFilterProps) {
  // Local state is only the edit in flight: the text while the debounce runs, or
  // a mode picked before there is a name for it to travel with. It is rendered
  // in place of the props until a prop changes, and from then on the props are
  // the truth — Clear all, an applied preset, back/forward all show through.
  const [draft, setDraft] = useState<Draft | null>(null)
  const [suggestions, setSuggestions] = useState<TestNameSuggestion[]>([])
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const lookup = useRef(0)
  const editing = draft && draft.base.name === name && draft.base.mode === mode ? draft : null
  const base = editing ? editing.base : { name, mode }
  const text = editing ? editing.text : name
  const pickedMode = editing ? editing.mode : mode

  useEffect(() => () => {
    if (timer.current) clearTimeout(timer.current)
  }, [])

  const commit = (value: string, nextMode: TestNameMode) => {
    if (value === name && nextMode === mode) return
    onChange(value, nextMode)
  }

  const load = async (query: string) => {
    const mine = ++lookup.current
    const data = await api
      .get<{ test_names?: TestNameSuggestion[] }>(
        `/api/dashboard/test-names?q=${encodeURIComponent(query)}&limit=${SUGGESTION_LIMIT}`,
      )
      .catch(() => null)
    // An earlier answer must never replace a later one.
    if (mine !== lookup.current) return
    setSuggestions(data?.test_names ?? [])
  }

  const changeText = (value: string) => {
    setDraft({ text: value, mode: pickedMode, base })
    if (timer.current) clearTimeout(timer.current)
    timer.current = setTimeout(() => {
      timer.current = null
      commit(value, pickedMode)
      void load(value)
    }, DEBOUNCE_MS)
  }

  const changeMode = (nextMode: TestNameMode) => {
    // A mode is a choice, not typing: it commits (with the visible text) at once.
    if (timer.current) {
      clearTimeout(timer.current)
      timer.current = null
    }
    // A mode with no name is not a filter, so the URL drops it — the draft keeps
    // it, and it goes out with the next name the user types.
    setDraft({ text, mode: nextMode, base })
    commit(text, nextMode)
  }

  return (
    <div
      data-testid="test-name-filter"
      className="relative flex w-full items-center gap-2 sm:w-auto"
      // A suggestion is picked with the pointer kept on the box, so the list is
      // dropped only when focus really leaves the control.
      onBlur={() => setSuggestions([])}
    >
      <SearchInput
        value={text}
        onChange={changeText}
        placeholder="Filter by failing test name"
        className="w-full sm:w-64"
      />
      <Select value={pickedMode} onValueChange={(v) => changeMode(v as TestNameMode)}>
        <SelectTrigger aria-label="Test name match mode" className="w-full sm:w-auto">
          <span className="text-sm">Match: {modeLabel(pickedMode)}</span>
        </SelectTrigger>
        <SelectContent>
          {TEST_NAME_MODES.map((option) => (
            <SelectItem key={option.value} value={option.value}>
              {option.label}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
      {suggestions.length > 0 && (
        <div
          role="listbox"
          aria-label="Failing test names"
          className="absolute left-0 top-full z-50 mt-1 max-h-72 w-full overflow-y-auto rounded-xl border border-border-default bg-surface-card p-1 shadow-lg sm:w-64"
        >
          {suggestions.map((row) => (
            <button
              key={row.test_name}
              type="button"
              role="option"
              aria-selected={row.test_name === text}
              aria-label={row.test_name}
              // Keep focus on the box so the pick is not swallowed by the blur.
              onMouseDown={(e) => e.preventDefault()}
              onClick={() => {
                setSuggestions([])
                setDraft({ text: row.test_name, mode: pickedMode, base })
                commit(row.test_name, pickedMode)
              }}
              className="flex w-full items-center justify-between gap-2 rounded-lg px-3 py-1.5 text-left text-sm text-text-primary hover:bg-surface-hover"
            >
              <span className="truncate font-mono text-xs">{row.test_name}</span>
              <span className="shrink-0 font-mono text-xs text-text-tertiary">
                {row.failure_count}
              </span>
            </button>
          ))}
        </div>
      )}
    </div>
  )
}
