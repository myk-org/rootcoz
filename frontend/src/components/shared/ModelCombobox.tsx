import { useState, useRef, useEffect, useCallback, useId } from 'react'
import { cn } from '@/lib/utils'
import { ChevronDown } from 'lucide-react'

export interface ModelOption {
  id: string
  name: string
}

interface ModelComboboxProps {
  value: string
  onChange: (value: string) => void
  options: ModelOption[]
  placeholder?: string
  className?: string
}

export function ModelCombobox({
  value,
  onChange,
  options,
  placeholder = 'Default model',
  className,
}: ModelComboboxProps) {
  const [open, setOpen] = useState(false)
  const [highlightIndex, setHighlightIndex] = useState(-1)
  const containerRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLInputElement>(null)
  const listRef = useRef<HTMLUListElement>(null)
  const listboxId = useId()

  // Fuzzy filter: case-insensitive substring match on id or name
  const filtered = options.filter((m) => {
    if (!value) return true
    const q = value.toLowerCase()
    return m.id.toLowerCase().includes(q) || m.name.toLowerCase().includes(q)
  })

  // Close on outside click
  useEffect(() => {
    function handleClick(e: MouseEvent) {
      if (containerRef.current && !containerRef.current.contains(e.target as Node)) {
        setOpen(false)
      }
    }
    if (open) {
      document.addEventListener('mousedown', handleClick)
      return () => document.removeEventListener('mousedown', handleClick)
    }
  }, [open])

  // Reset highlight when filtered list changes
  useEffect(() => {
    setHighlightIndex(-1)
  }, [value, open])

  // Scroll highlighted item into view
  useEffect(() => {
    if (highlightIndex >= 0 && listRef.current) {
      const items = listRef.current.children
      if (items[highlightIndex]) {
        (items[highlightIndex] as HTMLElement).scrollIntoView({ block: 'nearest' })
      }
    }
  }, [highlightIndex])

  const selectModel = useCallback(
    (id: string) => {
      onChange(id)
      setOpen(false)
      inputRef.current?.blur()
    },
    [onChange],
  )

  const handleKeyDown = useCallback(
    (e: React.KeyboardEvent) => {
      if (!open && (e.key === 'ArrowDown' || e.key === 'ArrowUp')) {
        setOpen(true)
        e.preventDefault()
        return
      }
      if (!open) return

      switch (e.key) {
        case 'ArrowDown':
          e.preventDefault()
          setHighlightIndex((prev) => (prev < filtered.length - 1 ? prev + 1 : 0))
          break
        case 'ArrowUp':
          e.preventDefault()
          setHighlightIndex((prev) => (prev > 0 ? prev - 1 : filtered.length - 1))
          break
        case 'Enter':
          e.preventDefault()
          if (highlightIndex >= 0 && highlightIndex < filtered.length) {
            selectModel(filtered[highlightIndex].id)
          } else {
            setOpen(false)
          }
          break
        case 'Escape':
          e.preventDefault()
          setOpen(false)
          break
        case 'Tab':
          setOpen(false)
          break
      }
    },
    [open, filtered, highlightIndex, selectModel],
  )

  const showDropdown = open && filtered.length > 0

  return (
    <div ref={containerRef} className={cn('relative', className)}>
      <div className="relative">
        <input
          ref={inputRef}
          type="text"
          className="flex h-9 w-full rounded-md border border-border-default bg-surface-elevated px-3 pr-8 py-1 text-sm text-text-primary shadow-sm transition-colors placeholder:text-text-tertiary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-border-accent disabled:cursor-not-allowed disabled:opacity-50"
          placeholder={placeholder}
          value={value}
          onChange={(e) => {
            onChange(e.target.value)
            if (!open) setOpen(true)
          }}
          onFocus={() => setOpen(true)}
          onClick={() => setOpen(true)}
          onKeyDown={handleKeyDown}
          role="combobox"
          aria-expanded={showDropdown}
          aria-haspopup="listbox"
          aria-autocomplete="list"
          aria-controls={listboxId}
          aria-activedescendant={
            highlightIndex >= 0 && filtered[highlightIndex]
              ? `${listboxId}-opt-${highlightIndex}`
              : undefined
          }
          autoComplete="off"
        />
        <button
          type="button"
          tabIndex={-1}
          className="absolute right-0 top-0 flex h-9 w-8 items-center justify-center text-text-tertiary hover:text-text-secondary transition-colors"
          onMouseDown={(e) => e.preventDefault()}
          onClick={() => {
            setOpen((prev) => {
              const next = !prev
              if (next) inputRef.current?.focus()
              return next
            })
          }}
          aria-label="Toggle model list"
        >
          <ChevronDown
            className={cn(
              'h-4 w-4 transition-transform duration-150',
              showDropdown && 'rotate-180',
            )}
          />
        </button>
      </div>

      {showDropdown && (
        <ul
          ref={listRef}
          role="listbox"
          id={listboxId}
          className="absolute z-50 mt-1 max-h-56 w-full overflow-auto rounded-md border border-border-default bg-surface-card shadow-lg animate-fade-in"
        >
          {filtered.map((model, i) => (
            <li
              key={model.id}
              id={`${listboxId}-opt-${i}`}
              role="option"
              aria-selected={model.id === value}
              className={cn(
                'flex cursor-default select-none items-center justify-between gap-2 px-3 py-1.5 text-sm transition-colors',
                i === highlightIndex
                  ? 'bg-surface-hover text-text-primary'
                  : 'text-text-primary hover:bg-surface-hover',
                model.id === value && 'font-medium',
              )}
              onMouseEnter={() => setHighlightIndex(i)}
              onMouseDown={(e) => {
                e.preventDefault() // prevent input blur
                selectModel(model.id)
              }}
            >
              <span className="truncate">{model.id}</span>
              {model.name && model.name !== model.id && (
                <span className="flex-shrink-0 text-xs text-text-tertiary truncate max-w-[40%]">
                  {model.name}
                </span>
              )}
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}
