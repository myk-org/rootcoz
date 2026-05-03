import { useState, useRef, useEffect, useCallback, type KeyboardEvent, type ChangeEvent } from 'react'
import { Textarea } from '@/components/ui/textarea'
import { api } from '@/lib/api'

/* ------------------------------------------------------------------ */
/*  Types                                                              */
/* ------------------------------------------------------------------ */

interface MentionTextareaProps {
  value: string
  onChange: (value: string) => void
  onSubmit: () => void
  placeholder?: string
  disabled?: boolean
}

interface MentionQuery {
  /** Start index of the `@` character in the textarea value. */
  start: number
  /** The partial username typed so far (after `@`). */
  query: string
}

/* ------------------------------------------------------------------ */
/*  Module-level cache for mentionable users                           */
/* ------------------------------------------------------------------ */

let cachedUsers: string[] | null = null
let fetchPromise: Promise<string[]> | null = null

async function fetchMentionableUsers(): Promise<string[]> {
  if (cachedUsers) return cachedUsers
  if (fetchPromise) return fetchPromise
  fetchPromise = api
    .get<{ usernames: string[] }>('/api/users/mentionable')
    .then((res) => {
      cachedUsers = res.usernames ?? []
      return cachedUsers
    })
    .catch(() => {
      fetchPromise = null
      return []
    })
  return fetchPromise
}

/** Exported for testing only — resets the module-level cache. */
export function _resetMentionCache() {
  cachedUsers = null
  fetchPromise = null
}

/* ------------------------------------------------------------------ */
/*  Helpers                                                            */
/* ------------------------------------------------------------------ */

/**
 * Detect an active @mention query at the current cursor position.
 * Returns null if the cursor is not inside a `@…` token, or if there's
 * a space between the `@` and the cursor.
 */
function detectMentionQuery(text: string, cursorPos: number): MentionQuery | null {
  // Walk backwards from cursor to find the `@` trigger
  const before = text.slice(0, cursorPos)
  const atIdx = before.lastIndexOf('@')
  if (atIdx === -1) return null
  // `@` must be at start of input or preceded by whitespace
  if (atIdx > 0 && !/\s/.test(before[atIdx - 1])) return null
  const query = before.slice(atIdx + 1)
  // No spaces allowed in the partial query
  if (/\s/.test(query)) return null
  return { start: atIdx, query }
}

/* ------------------------------------------------------------------ */
/*  Component                                                          */
/* ------------------------------------------------------------------ */

export function MentionTextarea({ value, onChange, onSubmit, placeholder, disabled }: MentionTextareaProps) {
  const textareaRef = useRef<HTMLTextAreaElement>(null)
  const dropdownRef = useRef<HTMLDivElement>(null)
  const [allUsers, setAllUsers] = useState<string[]>(cachedUsers ?? [])
  const [mentionQuery, setMentionQuery] = useState<MentionQuery | null>(null)
  const [selectedIdx, setSelectedIdx] = useState(0)

  // Fetch users on mount (cached after first successful call)
  useEffect(() => {
    void fetchMentionableUsers().then((users) => {
      if (users.length > 0) setAllUsers(users)
    })
  }, [])

  // Filtered list of matching users
  const filtered = mentionQuery
    ? allUsers.filter((u) => u.toLowerCase().startsWith(mentionQuery.query.toLowerCase()))
    : []

  // Keep selectedIdx in bounds
  useEffect(() => {
    setSelectedIdx(0)
  }, [mentionQuery?.query])

  /** Update the mention query state whenever the cursor moves or text changes. */
  const syncMentionQuery = useCallback(() => {
    const el = textareaRef.current
    if (!el) return
    const detected = detectMentionQuery(el.value, el.selectionStart)
    setMentionQuery(detected)
  }, [])

  /** Insert the selected username at the @-trigger position. */
  const insertMention = useCallback(
    (username: string) => {
      if (!mentionQuery) return
      const before = value.slice(0, mentionQuery.start)
      const after = value.slice(mentionQuery.start + 1 + mentionQuery.query.length)
      const insert = `@${username} `
      const newValue = before + insert + after
      onChange(newValue)
      setMentionQuery(null)
      // Restore cursor position after React re-render
      const cursorPos = before.length + insert.length
      requestAnimationFrame(() => {
        const el = textareaRef.current
        if (el) {
          el.focus()
          el.setSelectionRange(cursorPos, cursorPos)
        }
      })
    },
    [mentionQuery, value, onChange],
  )

  function handleChange(e: ChangeEvent<HTMLTextAreaElement>) {
    onChange(e.target.value)
    // Defer query detection to after React has flushed the new value
    requestAnimationFrame(() => syncMentionQuery())
  }

  function handleKeyDown(e: KeyboardEvent<HTMLTextAreaElement>) {
    if (e.nativeEvent.isComposing) return

    // When dropdown is open, intercept navigation keys
    if (mentionQuery && filtered.length > 0) {
      if (e.key === 'ArrowDown') {
        e.preventDefault()
        setSelectedIdx((i) => (i + 1) % filtered.length)
        return
      }
      if (e.key === 'ArrowUp') {
        e.preventDefault()
        setSelectedIdx((i) => (i - 1 + filtered.length) % filtered.length)
        return
      }
      if (e.key === 'Enter' || e.key === 'Tab') {
        e.preventDefault()
        insertMention(filtered[selectedIdx])
        return
      }
      if (e.key === 'Escape') {
        e.preventDefault()
        setMentionQuery(null)
        return
      }
    }

    // Default Enter = submit
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      onSubmit()
    }
  }

  // Close dropdown on click outside
  useEffect(() => {
    if (!mentionQuery) return
    function handleClickOutside(e: MouseEvent) {
      if (
        dropdownRef.current &&
        !dropdownRef.current.contains(e.target as Node) &&
        textareaRef.current &&
        !textareaRef.current.contains(e.target as Node)
      ) {
        setMentionQuery(null)
      }
    }
    document.addEventListener('mousedown', handleClickOutside)
    return () => document.removeEventListener('mousedown', handleClickOutside)
  }, [mentionQuery])

  const showDropdown = mentionQuery !== null && filtered.length > 0

  return (
    <div className="relative flex-1">
      <Textarea
        ref={textareaRef}
        aria-label="Add a comment"
        value={value}
        onChange={handleChange}
        onSelect={syncMentionQuery}
        placeholder={placeholder}
        className="min-h-[36px] resize-none text-sm"
        rows={1}
        onKeyDown={handleKeyDown}
        disabled={disabled}
      />
      {showDropdown && (
        <div
          ref={dropdownRef}
          role="listbox"
          className="absolute bottom-full left-0 z-50 mb-1 max-h-40 w-56 overflow-y-auto rounded-md border border-border-default bg-surface-elevated shadow-lg"
        >
          {filtered.map((user, i) => (
            <button
              key={user}
              type="button"
              role="option"
              aria-selected={i === selectedIdx}
              className={`w-full px-3 py-1.5 text-left text-sm ${
                i === selectedIdx
                  ? 'bg-signal-blue/20 text-text-primary'
                  : 'text-text-secondary hover:bg-surface-hover'
              }`}
              onMouseDown={(e) => {
                // Prevent blur on textarea
                e.preventDefault()
                insertMention(user)
              }}
            >
              @{user}
            </button>
          ))}
        </div>
      )}
    </div>
  )
}
