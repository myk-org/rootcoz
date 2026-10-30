import { useCallback, useEffect, useMemo, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { api, ApiError } from '@/lib/api'
import { Input } from '@/components/ui/input'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import {
  Table,
  TableBody,
  TableCell,
  TableHeader,
  TableRow,
} from '@/components/ui/table'
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { formatCompactNumber } from '@/lib/format'
import { FileText, Download, Eye, Loader2, Search } from 'lucide-react'

interface TranscriptItem {
  session_id: string
  size_bytes: number
  modified_at: number
  turns: number
  context: {
    job_id?: string
    call_type?: string
    ai_provider?: string
    ai_model?: string
    total_tokens?: number
    chat_username?: string
    chat_last_activity?: string
  }
}

interface TranscriptsResponse {
  recording_enabled: boolean
  count: number
  items: TranscriptItem[]
}

// UI display limit for long identifiers (session/job UUIDs) — named so the
// constraint is documented once instead of duplicated slice literals.
const ID_DISPLAY_LIMIT = 13

const shortId = (id: string): string => `${id.slice(0, ID_DISPLAY_LIMIT)}…`

// SQLite CURRENT_TIMESTAMP is timezone-less UTC in `YYYY-MM-DD HH:MM:SS`;
// Date.parse would read it as local time. Normalize to an explicit UTC ISO
// string before parsing so the tooltip is correct for admins outside UTC.
const parseDbTimestamp = (value: string): number =>
  Date.parse(value.includes('T') ? value : `${value.trim().replace(' ', 'T')}Z`)

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KiB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MiB`
}

function formatDate(epochSeconds: number): string {
  return new Date(epochSeconds * 1000).toLocaleString()
}

function contextSummary(item: TranscriptItem): string {
  const ctx = item.context ?? {}
  const bits: string[] = []
  if (ctx.call_type) bits.push(ctx.call_type)
  if (ctx.chat_username) bits.push(`chat: ${ctx.chat_username}`)
  if (ctx.ai_provider) bits.push(ctx.ai_provider)
  if (ctx.ai_model) bits.push(ctx.ai_model)
  if (ctx.total_tokens)
    bits.push(`${formatCompactNumber(ctx.total_tokens)} tokens`)
  return bits.length > 0 ? bits.join(' · ') : '—'
}

export function AdminTranscriptsPage() {
  const [data, setData] = useState<TranscriptsResponse | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [jobFilter, setJobFilter] = useState('')
  const [appliedFilter, setAppliedFilter] = useState('')
  const [viewing, setViewing] = useState<string | null>(null)
  const [viewContent, setViewContent] = useState('')
  const [viewLoading, setViewLoading] = useState(false)
  const [viewError, setViewError] = useState('')

  const load = useCallback(async (jobId: string) => {
    setLoading(true)
    setError('')
    try {
      const query = jobId ? `?job_id=${encodeURIComponent(jobId)}` : ''
      setData(await api.get<TranscriptsResponse>(`/api/admin/transcripts${query}`))
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load transcripts')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void load('')
  }, [load])

  const items = useMemo(() => data?.items ?? [], [data])

  const openViewer = useCallback(async (sessionId: string) => {
    setViewing(sessionId)
    setViewContent('')
    setViewError('')
    setViewLoading(true)
    try {
      setViewContent(await api.getText(`/api/admin/transcripts/${sessionId}`))
    } catch (err) {
      // A pruned session is the common answer, not an outage: say so instead of
      // echoing an HTTP status the admin cannot act on.
      setViewError(
        err instanceof ApiError && err.status === 404
          ? 'not found — this transcript was removed by the sidecar’s retention, or never existed.'
          : err instanceof Error
            ? err.message
            : 'Failed to load transcript',
      )
    } finally {
      setViewLoading(false)
    }
  }, [])

  // ?session=<id> is an audit row's target: open that transcript straight away.
  const [searchParams] = useSearchParams()
  const sessionParam = searchParams.get('session') ?? ''
  useEffect(() => {
    if (sessionParam) void openViewer(sessionParam)
  }, [sessionParam, openViewer])

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold">Session Transcripts</h1>
          <p className="text-sm text-muted-foreground">
            Recorded pi-sidecar sessions (redacted JSONL).{' '}
            {data ? `${data.count} recorded · ` : ''}Admin only, read-only.
          </p>
        </div>
      </div>

      {data && !data.recording_enabled && (
        <Card>
          <CardContent className="text-sm text-muted-foreground py-3">
            Transcript recording is currently disabled. Enable it in Server
            Settings → AI — it applies immediately, no restart needed
            (pi-sidecar ≥ 4.14.0).
          </CardContent>
        </Card>
      )}

      <form
        className="flex gap-2 items-center"
        onSubmit={(e) => {
          e.preventDefault()
          setAppliedFilter(jobFilter.trim())
          void load(jobFilter.trim())
        }}
      >
        <Input
          placeholder="Filter by job id…"
          value={jobFilter}
          onChange={(e) => setJobFilter(e.target.value)}
          className="max-w-sm"
        />
        <Button type="submit" variant="secondary" size="sm" disabled={loading}>
          <Search className="h-4 w-4 mr-1" /> Filter
        </Button>
        {appliedFilter && (
          <Button
            type="button"
            variant="ghost"
            size="sm"
            onClick={() => {
              setJobFilter('')
              setAppliedFilter('')
              void load('')
            }}
          >
            Clear
          </Button>
        )}
      </form>

      {error && <p className="text-sm text-destructive">{error}</p>}

      {loading ? (
        <div className="flex items-center gap-2 text-muted-foreground">
          <Loader2 className="h-4 w-4 animate-spin" /> Loading transcripts…
        </div>
      ) : (
        <Card>
          <CardContent className="p-0">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableCell>Session</TableCell>
                  <TableCell>Turns</TableCell>
                  <TableCell>Size</TableCell>
                  <TableCell>Last modified</TableCell>
                  <TableCell>Context</TableCell>
                  <TableCell>Job</TableCell>
                  <TableCell className="text-right">Actions</TableCell>
                </TableRow>
              </TableHeader>
              <TableBody>
                {items.length === 0 && (
                  <TableRow>
                    <TableCell colSpan={7} className="text-center text-muted-foreground py-8">
                      No transcripts{appliedFilter ? ' for this job' : ''}.
                    </TableCell>
                  </TableRow>
                )}
                {items.map((item) => (
                  <TableRow key={item.session_id}>
                    <TableCell className="font-mono text-xs">
                      {shortId(item.session_id)}
                    </TableCell>
                    <TableCell>{formatCompactNumber(item.turns)}</TableCell>
                    <TableCell>{formatSize(item.size_bytes)}</TableCell>
                    <TableCell className="text-sm text-muted-foreground">
                      {formatDate(item.modified_at)}
                    </TableCell>
                    <TableCell
                      className="text-sm"
                      title={
                        item.context?.chat_last_activity
                          ? `Last chat activity: ${formatDate(
                              parseDbTimestamp(item.context.chat_last_activity) / 1000,
                            )}`
                          : undefined
                      }
                    >
                      {contextSummary(item)}
                    </TableCell>
                    <TableCell className="text-sm">
                      {item.context?.job_id ? (
                        <a
                          className="text-primary hover:underline"
                          href={`/results/${item.context.job_id}`}
                        >
                          {shortId(item.context.job_id)}
                        </a>
                      ) : (
                        '—'
                      )}
                    </TableCell>
                    <TableCell className="text-right">
                      <div className="flex justify-end gap-1">
                        <Button
                          variant="ghost"
                          size="sm"
                          onClick={() => void openViewer(item.session_id)}
                          title="View transcript"
                        >
                          <Eye className="h-4 w-4" />
                        </Button>
                        <a
                          href={`/api/admin/transcripts/${item.session_id}`}
                          download={`${item.session_id}.jsonl`}
                        >
                          <Button variant="ghost" size="sm" title="Download JSONL">
                            <Download className="h-4 w-4" />
                          </Button>
                        </a>
                      </div>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </CardContent>
        </Card>
      )}

      <Dialog open={viewing !== null} onOpenChange={(open) => !open && setViewing(null)}>
        <DialogContent className="max-w-4xl max-h-[80vh] flex flex-col">
          <DialogHeader>
            <DialogTitle className="font-mono text-sm flex items-center gap-2">
              <FileText className="h-4 w-4" /> {viewing}.jsonl
            </DialogTitle>
          </DialogHeader>
          <div className="flex-1 overflow-auto">
            {viewLoading && (
              <div className="flex items-center gap-2 text-muted-foreground">
                <Loader2 className="h-4 w-4 animate-spin" /> Loading transcript…
              </div>
            )}
            {viewError && <p className="text-sm text-destructive">{viewError}</p>}
            {!viewLoading && !viewError && viewContent && (
              <pre className="text-xs whitespace-pre-wrap break-all font-mono">
                {viewContent}
              </pre>
            )}
          </div>
        </DialogContent>
      </Dialog>
    </div>
  )
}
