import { useState, useRef, useEffect, useLayoutEffect, useCallback, useId } from 'react'
import { createPortal } from 'react-dom'
import { cn } from '@/lib/utils'
import { CONTROL_CLASS } from '@/lib/controlStyles'
import { credentialLabel } from '@/lib/analysisAi'
import { CredentialSourceLabel, ServerAccessHint, ServerAccessTooltip } from '@/components/shared/CredentialSourceLabel'
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from '@/components/ui/tooltip'
import { ChevronDown } from 'lucide-react'

export interface ModelOption {
  id: string
  name: string
  /** Model source under the friendly provider: acpx | cli | api */
  source?: string
  credential_sources?: Array<'user' | 'server'>
  verified?: boolean
  serverLocked?: boolean
  /** Admin-declared free pair (see AiModel.free). */
  free?: boolean
  /** False when the sidecar will refuse to open a session for this pair
   *  (see AiModel.session_supported). */
  session_supported?: boolean
  /** Why a session cannot start — shown on the row, never as a native title. */
  session_unsupported_reason?: string
}

/** A model the sidecar would refuse a session for: not selectable, and the row
 *  carries the reason instead of failing at Start. Opt-in via the prop. */
const sessionLocked = (model: ModelOption, disableUnsessionable: boolean) => disableUnsessionable && model.session_supported === false
const isLocked = (model: ModelOption, disableUnsessionable: boolean) => Boolean(model.serverLocked) || sessionLocked(model, disableUnsessionable)

interface ModelComboboxProps {
  value: string
  onChange: (value: string) => void
  /** The value is final (pick, blur or Enter); onChange alone fires per keystroke. */
  onCommit?: (value: string) => void
  options: ModelOption[]
  placeholder?: string
  className?: string
  ariaLabel?: string
  disabled?: boolean
  forceServer?: boolean
  canUseServer?: boolean
  /** Free-granted users may use admin-declared free server models. */
  canUseFree?: boolean
  /** Refuse a model the sidecar would not open a session with. Absent means
   *  today's behaviour: opt-in here, never defaulted on, so the key-verification
   *  pickers keep offering a listed-but-undeclared id. */
  disableUnsessionable?: boolean
}

interface DropdownCoords {
  top: number
  left: number
  minWidth: number
}

export function ModelCombobox({
  value,
  onChange,
  onCommit,
  options,
  placeholder = 'Default model',
  className,
  ariaLabel,
  disabled = false,
  forceServer = false,
  canUseServer = true,
  canUseFree = false,
  disableUnsessionable = false,
}: ModelComboboxProps) {
  const [open, setOpen] = useState(false)
  const [highlight, setHighlight] = useState<{ key: string; index: number }>({ key: '', index: -1 })
  const [coords, setCoords] = useState<DropdownCoords | null>(null)
  const containerRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLInputElement>(null)
  const listRef = useRef<HTMLUListElement>(null)
  const listboxId = useId()
  // What a blur should commit: the user's latest intent. A pick or a keystroke knows
  // it before React re-renders, and the blur fires before that render lands.
  const committedValueRef = useRef(value)
  useEffect(() => {
    committedValueRef.current = value
  }, [value])
  const hasDeniedServer = !canUseServer && options.some((model) => model.credential_sources?.includes('server') && !(canUseFree && model.free))
  // Fuzzy filter: case-insensitive substring match on id or name. When the
  // current value matches nothing (a stale stored model, or the user typing a
  // new id), fall back to the full list — otherwise the dropdown is empty and
  // the user can neither type nor pick anything.
  const matches = options.filter((m) => {
    if (!value || options.some((m) => m.id === value)) return true
    const q = value.toLowerCase()
    return m.id.toLowerCase().includes(q) || m.name.toLowerCase().includes(q)
  })
  const filtered = matches.length > 0 ? matches : options

  const showDropdown = !disabled && open && filtered.length > 0

  // Position is only meaningful while the dropdown is mounted; derive `null`
  // when closed instead of clearing it from the layout effect.
  const activeCoords = showDropdown ? coords : null

  // Changing the value or opening/closing invalidates the keyboard highlight,
  // so it is stored per value+open key and derived back to -1 when stale.
  const highlightKey = `${value}|${open}`
  const highlightIndex = highlight.key === highlightKey ? highlight.index : -1
  const setHighlightIndex = useCallback(
    (next: number | ((prev: number) => number)) => {
      setHighlight((prev) => {
        const current = prev.key === highlightKey ? prev.index : -1
        return { key: highlightKey, index: typeof next === 'function' ? next(current) : next }
      })
    },
    [highlightKey],
  )

  // Position dropdown from trigger (portal to body — outside dialog transform /
  // RemoveScroll) and keep it aligned on scroll/resize.
  useLayoutEffect(() => {
    if (!showDropdown || !containerRef.current) return
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
  }, [showDropdown, activeCoords])

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

  // Scroll highlighted item into view
  useEffect(() => {
    if (highlightIndex >= 0 && listRef.current) {
      const items = listRef.current.children
      if (items[highlightIndex]) {
        ;(items[highlightIndex] as HTMLElement).scrollIntoView({ block: 'nearest' })
      }
    }
  }, [highlightIndex])

  // A typed id of a refused row must not slip past the list's lock, but only at the
  // commit points (blur, Enter): refusing every keystroke made a usable id that
  // extends a refused one ("gpt-5-mini" behind "gpt-5") impossible to type.
  const refusedValue = useCallback(
    (id: string) => {
      const option = options.find((m) => m.id === id)
      return option ? isLocked(option, disableUnsessionable) : false
    },
    [options, disableUnsessionable],
  )

  const commitTyped = useCallback(() => {
    if (!refusedValue(committedValueRef.current)) onCommit?.(committedValueRef.current)
  }, [refusedValue, onCommit])

  const selectModel = useCallback(
    (id: string) => {
      committedValueRef.current = id
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
          if (highlightIndex >= 0 && highlightIndex < filtered.length && !isLocked(filtered[highlightIndex], disableUnsessionable)) {
            selectModel(filtered[highlightIndex].id)
          } else {
            setOpen(false)
            // Enter on a free-typed id commits it, same as blur.
            commitTyped()
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
    [open, filtered, highlightIndex, selectModel, setHighlightIndex, commitTyped, disableUnsessionable],
  )

  const dropdown =
    activeCoords
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
              top: activeCoords.top,
              left: activeCoords.left,
              minWidth: activeCoords.minWidth,
              pointerEvents: 'auto',
            }}
            // w-max: grow to full model id text; max-w keeps it on-screen.
            // Portaled so it does not stretch the dialog / create a page scrollbar.
            className="z-[200] max-h-56 w-max max-w-[min(90vw,48rem)] overflow-y-auto overflow-x-hidden rounded-xl border border-border-default bg-surface-card shadow-lg backdrop-blur-sm animate-fade-in"
          >
            {filtered.map((model, i) => {
              const locked = isLocked(model, disableUnsessionable)
              const row = (
              <li
                id={`${listboxId}-opt-${i}`}
                role="option"
                aria-selected={model.id === value}
                className={cn(
                  'flex cursor-default select-none items-center justify-between gap-3 px-3 py-1.5 text-sm transition-colors whitespace-nowrap',
                  i === highlightIndex
                    ? 'bg-surface-hover text-text-primary'
                    : 'text-text-primary hover:bg-surface-hover',
                  model.id === value && 'font-medium',
                  locked && 'cursor-not-allowed opacity-60',
                )}
                onMouseEnter={() => setHighlightIndex(i)}
                aria-disabled={locked || undefined}
                onMouseDown={(e) => {
                  e.preventDefault() // prevent input blur
                  if (!locked) selectModel(model.id)
                }}
              >
                <span>{model.id}</span>
                <span className="flex shrink-0 items-center gap-2">
                  {sessionLocked(model, disableUnsessionable) && (
                    <span className="rounded border border-border-default px-1.5 py-0.5 text-[10px] uppercase tracking-wide text-text-tertiary">
                      NO SESSION
                    </span>
                  )}
                  {model.verified === false && <span className="text-xs text-signal-amber">UNVERIFIED</span>}
                  {model.free === true && (
                    <span className="rounded border border-signal-green px-1.5 py-0.5 text-[10px] uppercase tracking-wide text-signal-green" title="Admin-declared free model">
                      Free
                    </span>
                  )}
                  {model.verified !== false && model.credential_sources?.length ? (
                    <span className="text-xs text-text-secondary">
                      <CredentialSourceLabel label={credentialLabel([{ ...model, provider: '' }], false, forceServer)} canUseServer={canUseServer} />
                    </span>
                  ) : null}
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
              )
              if (sessionLocked(model, disableUnsessionable)) {
                // The reason is the row's whole point, so it replaces the
                // server-access tooltip rather than nesting inside it.
                return (
                  <TooltipProvider key={model.id} delayDuration={200}>
                    <Tooltip>
                      <TooltipTrigger asChild>{row}</TooltipTrigger>
                      <TooltipContent>{model.session_unsupported_reason}</TooltipContent>
                    </Tooltip>
                  </TooltipProvider>
                )
              }
              return (
                <ServerAccessTooltip key={model.id} show={!canUseServer && !(canUseFree && model.free) && !!model.credential_sources?.includes('server')}>
                  {row}
                </ServerAccessTooltip>
              )
            })}
          </ul>,
          document.body,
        )
      : null

  return (
    <div ref={containerRef} className={cn('relative', className)}>
      {hasDeniedServer && <ServerAccessHint id={`${listboxId}-server-help`} />}
      <div className="relative">
        <input
          ref={inputRef}
          type="text"
          className={cn(
            CONTROL_CLASS,
            'flex w-full px-4 pr-8 py-1 text-sm text-text-primary transition-colors placeholder:text-text-tertiary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-border-accent focus-visible:ring-offset-1 disabled:cursor-not-allowed disabled:opacity-50',
          )}
          placeholder={placeholder}
          value={value}
          aria-label={ariaLabel}
          aria-describedby={hasDeniedServer ? `${listboxId}-server-help` : undefined}
          disabled={disabled}
          onChange={(e) => {
            // Every keystroke lands, even one that spells a refused id: the lock
            // is on the commit points (blur, Enter, list pick), because a refused
            // id may be the prefix of a usable one ("gpt-5" / "gpt-5-mini").
            // Submitted value stays gated by isAnalysisAiAvailable.
            committedValueRef.current = e.target.value
            onChange(e.target.value)
            if (!open) setOpen(true)
          }}
          onBlur={commitTyped}
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
          aria-label={ariaLabel ? `Toggle ${ariaLabel} list` : 'Toggle model list'}
          disabled={disabled}
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
