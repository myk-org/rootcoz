import * as React from "react"
import { Check, ChevronDown, X } from "lucide-react"
import { cn } from "@/lib/utils"
import { CONTROL_CLASS } from "@/lib/controlStyles"
import { Input } from "@/components/ui/input"
import { Popover, PopoverAnchor, PopoverContent } from "@/components/ui/popover"

export interface SearchableMultiSelectOption {
  value: string
  /** Text the admin reads, when the value itself is a machine id. */
  label?: string
  /** Why this option cannot be picked (an exporter that is not configured). */
  hint?: string
  disabled?: boolean
}

interface SearchableMultiSelectProps {
  /** Selection, in the order it was picked. */
  values: string[]
  options: SearchableMultiSelectOption[]
  onChange: (values: string[]) => void
  /** Names the control: `Add <label>`, `Select <label>`, `Remove <value>`, `Search <label>`. */
  label: string
  /** Every enumerated setting uses this one control; single choice closes on pick. */
  multiple?: boolean
  /** The runtime list is still in flight — the menu says so instead of lying. */
  loading?: boolean
  /** Free entry: an SSO user the user table does not list yet. */
  allowCustom?: boolean
  disabled?: boolean
  placeholder?: string
  /** Trigger width override (long labels); the default fits a short value. */
  className?: string
}

/**
 * Case-insensitive subsequence score: `adm` matches `admin` and `api-gw`
 * matches `api-gateway`. A prefix match sorts before every other match (-1),
 * the rest by where their first matched character sits (earlier first).
 * Returns null when the query is not a subsequence of the text.
 */
function matchScore(text: string, query: string): number | null {
  if (!query) return 0
  const haystack = text.toLowerCase()
  const needle = query.toLowerCase()
  let from = 0
  for (const char of needle) {
    const at = haystack.indexOf(char, from)
    if (at < 0) return null
    from = at + 1
  }
  return haystack.startsWith(needle) ? -1 : haystack.indexOf(needle[0])
}

interface Row {
  value: string
  label?: string
  hint?: string
  disabled?: boolean
  /** The `Add "<query>"` row (free entry, not a server choice). */
  custom?: boolean
}

export function SearchableMultiSelect({
  values,
  options,
  onChange,
  label,
  multiple = true,
  loading = false,
  allowCustom = false,
  disabled = false,
  placeholder,
  className,
}: SearchableMultiSelectProps) {
  const emptyText = placeholder ?? (multiple ? "Add…" : "Select…")
  const [open, setOpen] = React.useState(false)
  const [query, setQuery] = React.useState("")
  const [highlight, setHighlight] = React.useState(0)
  const inputRef = React.useRef<HTMLInputElement>(null)
  const triggerRef = React.useRef<HTMLDivElement>(null)
  const listboxId = React.useId()

  const trimmed = query.trim()
  const ranked = options
    // The label is what the admin reads, so it is what they type.
    .map(option => ({ option, score: matchScore(option.label ?? option.value, trimmed) }))
    .filter(entry => entry.score !== null)
    // Stable sort: equal scores keep the order the server sent.
    .sort((a, b) => (a.score as number) - (b.score as number))
    .map(entry => entry.option)

  const exact = options.some(option => option.value.toLowerCase() === trimmed.toLowerCase())
  const rows: Row[] = ranked
  if (allowCustom && trimmed && !exact) rows.push({ value: trimmed, custom: true })

  // The highlight is a plain index into the current rows; a shorter list (the
  // admin kept typing) clamps it instead of pointing at nothing.
  const active = rows.length ? Math.min(highlight, rows.length - 1) : -1

  const setOpenState = (next: boolean) => {
    setOpen(next)
    setHighlight(0)
    if (!next) {
      setQuery("")
      // Escape/outside click drops focus with the menu: hand it back.
      triggerRef.current?.focus()
    }
  }

  const pick = (row: Row) => {
    if (row.disabled) return
    if (!multiple) {
      // One choice: the pick replaces the value and the menu is done.
      onChange([row.value])
      setOpenState(false)
      return
    }
    onChange(
      values.includes(row.value) ? values.filter(value => value !== row.value) : [...values, row.value],
    )
    // The menu stays open for the next pick, starting from a clean query.
    setQuery("")
    setHighlight(0)
    inputRef.current?.focus()
  }

  // Single mode shows the chosen value, never a chip: there is nothing to remove.
  const selectedOption = multiple ? undefined : options.find(option => option.value === values[0])
  const selectedText = multiple ? "" : selectedOption?.label ?? selectedOption?.value ?? values[0] ?? ""

  const handleKeyDown = (event: React.KeyboardEvent<HTMLInputElement>) => {
    if (event.key === "ArrowDown") {
      event.preventDefault()
      setHighlight(rows.length ? Math.min(active + 1, rows.length - 1) : 0)
    } else if (event.key === "ArrowUp") {
      event.preventDefault()
      setHighlight(Math.max(active - 1, 0))
    } else if (event.key === "Enter") {
      event.preventDefault()
      if (active >= 0) pick(rows[active])
    } else if (event.key === "Escape") {
      event.preventDefault()
      setOpenState(false)
    }
  }

  return (
    <Popover open={open} onOpenChange={setOpenState}>
      <PopoverAnchor asChild>
        {/* role=combobox on a focusable element that holds the chips: a button
            may not nest the chips' remove buttons. */}
        <div
          role="combobox"
          ref={triggerRef}
          tabIndex={disabled ? -1 : 0}
          aria-label={multiple ? `Add ${label}` : `Select ${label}`}
          aria-expanded={open}
          aria-haspopup="listbox"
          aria-disabled={disabled || undefined}
          onClick={() => setOpenState(!open)}
          onKeyDown={event => {
            if (event.key === "Enter" || event.key === " ") {
              event.preventDefault()
              setOpenState(!open)
            }
          }}
          className={cn(
            // This is a select trigger, so it wears the one shared control shape.
            CONTROL_CLASS,
            "flex w-full max-w-xs cursor-pointer items-center justify-between gap-2 overflow-hidden px-4 text-sm text-text-primary transition-colors focus:outline-none focus:ring-2 focus:ring-border-accent focus:ring-offset-1 aria-disabled:cursor-not-allowed aria-disabled:opacity-50",
            className,
          )}
        >
          {/* h-9 fixed, so empty and filled keep one height (nothing shifts). */}
          <span className="flex min-w-0 items-center gap-1.5 overflow-hidden">
            {!multiple ? (
              selectedText ? (
                <span className="truncate">{selectedText}</span>
              ) : (
                <span className="truncate text-text-tertiary">{emptyText}</span>
              )
            ) : values.length === 0 ? (
              <span className="truncate text-text-tertiary">{emptyText}</span>
            ) : (
              values.map(value => (
                <span
                  key={value}
                  className="inline-flex shrink-0 items-center gap-1 rounded-md border border-border-default bg-surface-card px-2 py-0.5 font-mono text-xs text-text-primary"
                >
                  {value}
                  <button
                    type="button"
                    aria-label={`Remove ${value}`}
                    disabled={disabled}
                    onClick={event => {
                      event.stopPropagation()
                      onChange(values.filter(v => v !== value))
                    }}
                    className="rounded text-text-tertiary transition-colors hover:text-signal-red"
                  >
                    <X className="h-3 w-3" />
                  </button>
                </span>
              ))
            )}
          </span>
          <ChevronDown
            className={cn("h-3.5 w-3.5 shrink-0 opacity-30 transition-transform", open && "rotate-180")}
          />
        </div>
      </PopoverAnchor>
      <PopoverContent
        align="start"
        sideOffset={4}
        className="w-[var(--radix-popover-trigger-width)] min-w-56 p-1"
        onOpenAutoFocus={event => {
          // The search field, never the menu chrome, takes the first focus.
          event.preventDefault()
          inputRef.current?.focus()
        }}
      >
        <Input
          ref={inputRef}
          value={query}
          onChange={event => {
            setQuery(event.target.value)
            setHighlight(0)
          }}
          onKeyDown={handleKeyDown}
          placeholder="Type to filter…"
          aria-label={`Search ${label}`}
          aria-controls={listboxId}
          aria-activedescendant={active >= 0 ? `${listboxId}-row-${active}` : undefined}
          role="combobox"
          aria-expanded
          autoComplete="off"
          className="mb-1 h-9 font-mono text-sm"
        />
        <ul
          id={listboxId}
          role="listbox"
          aria-multiselectable={multiple || undefined}
          aria-label={`${label} choices`}
          className="max-h-64 overflow-y-auto"
        >
          {loading && rows.length === 0 ? (
            <li className="px-3 py-2 text-xs italic text-text-tertiary">Loading choices…</li>
          ) : rows.length === 0 ? (
            <li className="px-3 py-2 text-xs italic text-text-tertiary">
              {trimmed ? "No matching choices" : "Nothing left to add"}
            </li>
          ) : (
            rows.map((row, index) => {
              const selected = !row.custom && values.includes(row.value)
              return (
                <li
                  key={row.custom ? `custom:${row.value}` : row.value}
                  id={`${listboxId}-row-${index}`}
                  role="option"
                  aria-selected={selected}
                  aria-disabled={row.disabled || undefined}
                  onMouseEnter={() => setHighlight(index)}
                  // preventDefault keeps focus (and the query) in the search field.
                  onMouseDown={event => {
                    event.preventDefault()
                    pick(row)
                  }}
                  className={cn(
                    "flex w-full cursor-default select-none items-center justify-between gap-2 rounded-lg px-3 py-1.5 font-mono text-sm text-text-primary outline-none",
                    index === active && "bg-surface-hover",
                    row.disabled && "pointer-events-none opacity-50",
                    row.custom && "font-sans italic text-text-secondary",
                  )}
                >
                  <span className="flex min-w-0 items-center gap-1.5 truncate">
                    <Check
                      className={cn("h-3.5 w-3.5 shrink-0", !selected && "opacity-0")}
                      aria-hidden
                    />
                    {row.custom ? `Add "${row.value}"` : row.label ?? row.value}
                  </span>
                  {row.hint && (
                    <span className="shrink-0 font-sans text-xs text-text-quaternary">{row.hint}</span>
                  )}
                </li>
              )
            })
          )}
        </ul>
      </PopoverContent>
    </Popover>
  )
}
