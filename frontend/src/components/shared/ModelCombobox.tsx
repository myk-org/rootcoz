import { useState, useRef, useEffect, useLayoutEffect, useCallback, useId } from 'react'
import { createPortal } from 'react-dom'
import { cn } from '@/lib/utils'
import { ChevronDown } from 'lucide-react'

export interface ModelOption {
  id: string
  name: string
  /** Model source under the friendly provider: acpx | cli | api */
  source?: string
}

interface ModelComboboxProps {
  value: string
  onChange: (value: string) => void
  options: ModelOption[]
  placeholder?: string
  className?: string
}

interface DropdownCoords {
  top: number
  left: number
  minWidth: number
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
  const [coords, setCoords] = useState<DropdownCoords | null>(null)
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

  const showDropdown = open && filtered.length > 0

  // Position dropdown from trigger (portal to body — outside dialog transform /
  // RemoveScroll) and keep it aligned on scroll/resize.
  useLayoutEffect(() => {
    if (!showDropdown || !containerRef.current) {
      setCoords(null)
      return
    }
    const update = () => {
      const rect = containerRef.current!.getBoundingClientRect()
      setCoords({
        top: rect.bottom + 4,
        left: rect.left,
        minWidth: rect.width,
      })
    }
    update()
    window.addEventListener('resize', update)
    // capture scroll from dialog/overflow ancestors
    window.addEventListener('scroll', update, true)
    return () => {
      window.removeEventListener('resize', update)
      window.removeEventListener('scroll', update, true)
    }
  }, [showDropdown, filtered.length])

  // Bypass Radix RemoveScroll: capture wheel before the lock cancels it.
  useEffect(() => {
    const el = listRef.current
    if (!el || !showDropdown) return
    const onWheel = (e: WheelEvent) => {
      e.preventDefault()
      e.stopPropagation()
      el.scrollTop += e.deltaY
    }
    el.addEventListener('wheel', onWheel, { passive: false, capture: true })
    return () => el.removeEventListener('wheel', onWheel, { capture: true })
  }, [showDropdown, coords])

  // Close on outside click (portal is outside containerRef)
  useEffect(() => {
    function handleClick(e: MouseEvent) {
      const t = e.target as Node
      if (containerRef.current?.contains(t) || listRef.current?.contains(t)) {
        return
      }
      setOpen(false)
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
        ;(items[highlightIndex] as HTMLElement).scrollIntoView({ block: 'nearest' })
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

  const dropdown =
    showDropdown && coords
      ? createPortal(
          <ul
            ref={listRef}
            role="listbox"
            id={listboxId}
            // Body is pointer-events:none while a Radix Dialog is open; restore
            // hits so scroll/select work for this body-portaled list.
            data-model-combobox-dropdown=""
            style={{
              position: 'fixed',
              top: coords.top,
              left: coords.left,
              minWidth: coords.minWidth,
              pointerEvents: 'auto',
            }}
            // w-max: grow to full model id text; max-w keeps it on-screen.
            // Portaled so it does not stretch the dialog / create a page scrollbar.
            className="z-[200] max-h-56 w-max max-w-[min(90vw,48rem)] overflow-y-auto overflow-x-hidden rounded-xl border border-border-default bg-surface-card shadow-lg backdrop-blur-sm animate-fade-in"
          >
            {filtered.map((model, i) => (
              <li
                key={model.id}
                id={`${listboxId}-opt-${i}`}
                role="option"
                aria-selected={model.id === value}
                className={cn(
                  'flex cursor-default select-none items-center justify-between gap-3 px-3 py-1.5 text-sm transition-colors whitespace-nowrap',
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
                <span>{model.id}</span>
                <span className="flex shrink-0 items-center gap-2">
                  {model.source && (
                    <span className="rounded border border-border-default px-1.5 py-0.5 text-[10px] uppercase tracking-wide text-text-tertiary">
                      {model.source}
                    </span>
                  )}
                  {model.name && model.name !== model.id && (
                    <span className="text-xs text-text-tertiary">{model.name}</span>
                  )}
                </span>
              </li>
            ))}
          </ul>,
          document.body,
        )
      : null

  return (
    <div ref={containerRef} className={cn('relative', className)}>
      <div className="relative">
        <input
          ref={inputRef}
          type="text"
          className="flex h-9 w-full rounded-full border border-border-default bg-surface-elevated px-4 pr-8 py-1 text-sm text-text-primary transition-colors placeholder:text-text-tertiary hover:bg-surface-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-border-accent focus-visible:ring-offset-1 disabled:cursor-not-allowed disabled:opacity-50"
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
          className="absolute right-0 top-0 flex h-9 w-7 items-center justify-center text-text-tertiary hover:text-text-secondary transition-colors"
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
              'h-3.5 w-3.5 opacity-30 transition-transform duration-150',
              showDropdown && 'rotate-180',
            )}
          />
        </button>
      </div>
      {dropdown}
    </div>
  )
}
