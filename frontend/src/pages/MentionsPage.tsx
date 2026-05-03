import { useCallback, useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { api } from '@/lib/api'
import { cn } from '@/lib/utils'
import { Skeleton } from '@/components/ui/skeleton'
import { Button } from '@/components/ui/button'
import { Pagination } from '@/components/shared/Pagination'
import { AtSign, CheckCheck } from 'lucide-react'

/* ------------------------------------------------------------------ */
/*  Types                                                              */
/* ------------------------------------------------------------------ */

interface Mention {
  id: number
  job_id: string
  test_name: string
  child_job_name: string
  child_build_number: number
  comment: string
  username: string
  created_at: string
  is_read: boolean
}

interface MentionsResponse {
  mentions: Mention[]
  total: number
  unread_count: number
}

/* ------------------------------------------------------------------ */
/*  Helpers                                                            */
/* ------------------------------------------------------------------ */

const LIMIT = 50

function timeAgo(dateStr: string): string {
  const date = new Date(dateStr.includes('T') ? dateStr : dateStr.replace(' ', 'T') + 'Z')
  const now = Date.now()
  const diffMs = now - date.getTime()
  if (diffMs < 0) return 'just now'
  const seconds = Math.floor(diffMs / 1000)
  if (seconds < 60) return 'just now'
  const minutes = Math.floor(seconds / 60)
  if (minutes < 60) return `${minutes}m ago`
  const hours = Math.floor(minutes / 60)
  if (hours < 24) return `${hours}h ago`
  const days = Math.floor(hours / 24)
  if (days < 30) return `${days}d ago`
  const months = Math.floor(days / 30)
  return `${months}mo ago`
}

/** Truncate text for list preview. Full text is available via expand-on-click.
 *  This is intentional UI summarization, not lossy data — the API always returns full text. */
function truncate(text: string, max: number): string {
  if (text.length <= max) return text
  return text.slice(0, max).trimEnd() + '…'
}

/* ------------------------------------------------------------------ */
/*  Component                                                          */
/* ------------------------------------------------------------------ */

export function MentionsPage() {
  const [mentions, setMentions] = useState<Mention[]>([])
  const [total, setTotal] = useState(0)
  const [unreadCount, setUnreadCount] = useState(0)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [page, setPage] = useState(1)
  const [expandedIds, setExpandedIds] = useState<Set<number>>(new Set())
  const [markingAll, setMarkingAll] = useState(false)
  const requestSeqRef = useRef(0)

  const fetchMentions = useCallback(async (p: number) => {
    const seq = ++requestSeqRef.current
    setLoading(true)
    setError(null)
    try {
      const params = new URLSearchParams({
        offset: String((p - 1) * LIMIT),
        limit: String(LIMIT),
        unread_only: 'false',
      })
      const res = await api.get<MentionsResponse>(`/api/users/mentions?${params}`)
      if (seq === requestSeqRef.current) {
        setMentions(res.mentions)
        setTotal(res.total)
        setUnreadCount(res.unread_count)
      }
    } catch (err) {
      if (seq === requestSeqRef.current) {
        setError(err instanceof Error ? err.message : 'Failed to load mentions')
      }
    } finally {
      if (seq === requestSeqRef.current) {
        setLoading(false)
      }
    }
  }, [])

  useEffect(() => {
    fetchMentions(page)
  }, [page, fetchMentions])

  // Cleanup on unmount
  useEffect(() => {
    return () => { requestSeqRef.current += 1 }
  }, [])

  function toggleExpand(id: number) {
    setExpandedIds((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  async function markAllRead() {
    setMarkingAll(true)
    try {
      await api.post('/api/users/mentions/read-all', {})
      // Refresh the current page to reflect the changes
      setMentions((prev) => prev.map((m) => ({ ...m, is_read: true })))
      setUnreadCount(0)
      window.dispatchEvent(new Event('mentions-updated'))
    } catch {
      // best-effort
    } finally {
      setMarkingAll(false)
    }
  }

  async function markRead(id: number) {
    const mention = mentions.find((m) => m.id === id)
    if (!mention || mention.is_read) return
    try {
      await api.post('/api/users/mentions/read', { comment_ids: [id] })
      setMentions((prev) =>
        prev.map((m) => (m.id === id ? { ...m, is_read: true } : m)),
      )
      setUnreadCount((prev) => Math.max(0, prev - 1))
      window.dispatchEvent(new Event('mentions-updated'))
    } catch {
      // best-effort
    }
  }

  const navigate = useNavigate()
  const totalPages = Math.max(1, Math.ceil(total / LIMIT))

  function buildMentionUrl(m: Mention): string {
    const params = new URLSearchParams({ comment: String(m.id) })
    if (m.child_job_name) params.set('child_job_name', m.child_job_name)
    if (m.child_build_number) params.set('child_build_number', String(m.child_build_number))
    return `/results/${m.job_id}?${params}`
  }

  function openMention(m: Mention) {
    markRead(m.id)
    navigate(buildMentionUrl(m))
  }

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <AtSign className="h-5 w-5 text-text-tertiary" />
          <h1 className="font-display text-xl font-bold text-text-primary">Mentions</h1>
          {unreadCount > 0 && (
            <span className="inline-flex items-center justify-center rounded-full bg-signal-blue px-2 py-0.5 text-[11px] font-semibold text-white">
              {unreadCount} unread
            </span>
          )}
        </div>
        {unreadCount > 0 && (
          <Button
            variant="outline"
            size="sm"
            onClick={markAllRead}
            disabled={markingAll}
            className="gap-1.5 text-xs"
          >
            <CheckCheck className="h-3.5 w-3.5" />
            Mark all as read
          </Button>
        )}
      </div>

      {/* Results count */}
      <div className="flex items-center">
        <span className="text-xs text-text-tertiary font-mono">
          {total} mention{total !== 1 ? 's' : ''}
        </span>
      </div>

      {/* Error */}
      {error && <p className="text-center text-signal-red py-8">{error}</p>}

      {/* Loading */}
      {loading ? (
        <div className="space-y-2">
          {Array.from({ length: 8 }).map((_, i) => (
            <Skeleton key={i} className="h-16 w-full" />
          ))}
        </div>
      ) : !error && mentions.length === 0 ? (
        <div className="flex items-center justify-center rounded-lg border border-border-muted bg-surface-card py-16 animate-fade-in">
          <p className="text-text-secondary">No mentions yet.</p>
        </div>
      ) : (
        <div className="space-y-2">
          {mentions.map((m, i) => {
            const isExpanded = expandedIds.has(m.id)
            const commentText = isExpanded ? m.comment : truncate(m.comment, 200)
            const needsTruncation = m.comment.length > 200

            return (
              <div
                key={m.id}
                tabIndex={0}
                role="button"
                className={cn(
                  'group rounded-lg border px-4 py-3 transition-colors duration-150 animate-slide-up cursor-pointer',
                  m.is_read
                    ? 'border-border-muted bg-surface-card opacity-60'
                    : 'border-border-muted border-l-2 border-l-signal-blue bg-signal-blue/10',
                )}
                onClick={() => openMention(m)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter' || e.key === ' ') {
                    e.preventDefault()
                    openMention(m)
                  }
                }}
                style={{
                  animationDelay: `${i * 30}ms`,
                  animationFillMode: 'backwards',
                }}
              >
                <div className="flex items-start gap-3">
                  {/* Unread dot */}
                  <div className="mt-1.5 shrink-0 w-2">
                    {!m.is_read && (
                      <span className="block h-2 w-2 rounded-full bg-signal-blue" title="Unread" />
                    )}
                  </div>

                  {/* Content */}
                  <div className="min-w-0 flex-1 space-y-1">
                    {/* Top row: author + time */}
                    <div className="flex items-center gap-2 flex-wrap">
                      <span className={cn(
                        'font-mono text-xs text-signal-blue',
                        !m.is_read && 'font-semibold',
                      )}>
                        @{m.username}
                      </span>
                      <span className="text-[10px] text-text-tertiary">
                        {timeAgo(m.created_at)}
                      </span>
                    </div>

                    {/* Comment text */}
                    <p
                      className={cn(
                        'text-sm text-text-secondary whitespace-pre-wrap',
                        needsTruncation && 'cursor-pointer',
                      )}
                      onClick={(e) => {
                        if (needsTruncation) {
                          e.stopPropagation()
                          toggleExpand(m.id)
                        }
                      }}
                    >
                      {commentText}
                    </p>

                    {/* Bottom row: job + test */}
                    <div className="flex items-center gap-3 flex-wrap text-xs text-text-tertiary">
                      <span className="text-text-link font-mono">
                        {m.job_id}
                      </span>
                      {m.test_name && (
                        <span className="font-mono truncate max-w-[400px]" title={m.test_name}>
                          {m.test_name}
                        </span>
                      )}
                    </div>
                  </div>
                </div>
              </div>
            )
          })}
        </div>
      )}

      <Pagination page={page} totalPages={totalPages} onPageChange={setPage} />
    </div>
  )
}
