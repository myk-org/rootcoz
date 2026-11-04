import { useCallback, useEffect, useRef, useState } from 'react'
import {
  Activity,
  AlertTriangle,
  Bot,
  Cpu,
  Database,
  HardDrive,
  MemoryStick,
  RefreshCw,
  Server,
  Timer,
} from 'lucide-react'
import { api } from '@/lib/api'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Table, TableBody, TableCell, TableRow } from '@/components/ui/table'
import { formatBytes, formatUptime } from '@/lib/format'
import { formatRelativeTime } from '@/lib/utils'

/**
 * Payload of GET /api/admin/server-status (src/rootcoz/server_status.py).
 *
 * Every probe block carries its own `available` flag and a `reason` when it
 * could not read what it needs — those blocks must render as "not visible",
 * never as zeroes that look healthy.
 */
interface ProbeBlock {
  available: boolean
  reason?: string
}

interface DiskBlock extends ProbeBlock {
  used_bytes?: number
  capacity_bytes?: number
  used_ratio?: number | null
  filesystem_note?: string
  database_bytes?: number | null
  transcripts_bytes?: number | null
  logs_bytes?: number | null
}

interface CpuBlock extends ProbeBlock {
  process_percent?: number
  quota_cores?: number | null
  /** "unknown" is a cpu.max that could not be read — never "unlimited". */
  quota_state?: 'set' | 'unlimited' | 'unknown'
  period_us?: number | null
}

interface MemoryBlock extends ProbeBlock {
  rss_bytes?: number | null
  current_bytes?: number
  working_set_bytes?: number | null
  limit_bytes?: number | null
  /** "unknown" is a memory.max that could not be read — never "no limit set". */
  limit_state?: 'set' | 'unlimited' | 'unknown'
  /** Working set as a share of the limit: what an OOM kill follows. */
  percent_of_limit?: number | null
}

interface DatabaseBlock extends ProbeBlock {
  size_bytes?: number | null
  results?: number
  failures?: number
  test_entries?: number
  /** Not reported until #409 lands; always shown as a placeholder. */
  compression_ratio?: number | null
}

interface SidecarBlock {
  available?: boolean
  reachable: boolean
  models: number | null
  free_models: number | null
  reason?: string
}

interface ProcessBlock {
  version: string
  git_sha: string
  uptime_seconds: number
  pid: number
  /** Not readable from inside a container; null, never 0. */
  restart_count?: number | null
  loop_lag_ms: number | null
}

interface RunningAnalysis {
  job_id: string
  job_name: string
  build_number: number
  status: string
  elapsed_seconds: number | null
}

interface ActivityBlock {
  analyses_running?: number
  analyses?: RunningAnalysis[]
  chat_turns_in_flight?: number
  sse_clients?: number
}

interface ServerWarning {
  level: string
  code: string
  message: string
}

export interface ServerStatus {
  disk: DiskBlock
  cpu: CpuBlock
  memory: MemoryBlock
  process: ProcessBlock
  database: DatabaseBlock
  sidecar: SidecarBlock
  activity: ActivityBlock
  warnings: ServerWarning[]
}

/** Auto-refresh cadence; a hidden tab skips its turn instead of polling. */
const REFRESH_MS = 30_000

const NOT_VISIBLE = 'not visible in this container'

const WARNING_STYLES: Record<
  string,
  { label: string; variant: 'destructive' | 'warning'; box: string; icon: string }
> = {
  critical: {
    label: 'Critical',
    variant: 'destructive',
    box: 'border-signal-red/30 bg-signal-red/10',
    icon: 'text-signal-red',
  },
  warn: {
    label: 'Warning',
    variant: 'warning',
    box: 'border-signal-orange/30 bg-signal-orange/10',
    icon: 'text-signal-orange',
  },
}

function NotVisible({ reason }: { reason?: string }) {
  return (
    <p className="text-sm text-text-tertiary">
      {NOT_VISIBLE}
      {reason ? ` — ${reason}` : ''}
    </p>
  )
}

/** One label/value pair inside a card. */
function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex items-baseline justify-between gap-4 py-0.5">
      <dt className="text-xs text-text-tertiary">{label}</dt>
      <dd className="text-right font-mono text-sm text-text-primary">{children}</dd>
    </div>
  )
}

function StatusCard({
  title,
  icon,
  children,
}: {
  title: string
  icon: React.ReactNode
  children: React.ReactNode
}) {
  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="flex items-center gap-2 font-display text-sm font-medium text-text-primary">
          <span className="text-text-tertiary">{icon}</span>
          {title}
        </CardTitle>
      </CardHeader>
      <CardContent>{children}</CardContent>
    </Card>
  )
}

/** A byte count, or a muted "not visible" — never a `0 B` for a missing probe. */
function bytesValue(bytes: number | null | undefined) {
  return bytes == null ? <NotVisible /> : formatBytes(bytes)
}

/** A percentage with one decimal, or a muted "not visible". */
function percentValue(percent: number | null | undefined) {
  return percent == null ? <NotVisible /> : `${percent.toFixed(1)}%`
}

/** A row count, or the caller's wording for a count the server could not take. */
function countValue(count: number | undefined, missing: string) {
  return count == null ? <span className="text-text-tertiary">{missing}</span> : String(count)
}

function WarningBanner({ warning }: { warning: ServerWarning }) {
  const style = WARNING_STYLES[warning.level] ?? WARNING_STYLES.warn
  return (
    <div
      role="alert"
      className={`flex items-start gap-3 rounded-lg border p-3 ${style.box}`}
    >
      <AlertTriangle className={`mt-0.5 h-4 w-4 shrink-0 ${style.icon}`} />
      <div className="flex min-w-0 flex-wrap items-center gap-2">
        <Badge variant={style.variant}>{style.label}</Badge>
        <span className="text-sm text-text-primary">{warning.message}</span>
      </div>
    </div>
  )
}

export function AdminServerStatusPage() {
  const [data, setData] = useState<ServerStatus | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [updatedAt, setUpdatedAt] = useState<number | null>(null)
  // Ticked every second so the "updated …" label stays true, not just at fetch.
  const [now, setNow] = useState(() => Date.now())
  const loadedAtRef = useRef(0)
  // One request at a time: a slow response must not be followed by another
  // request whose older snapshot could land last.
  const inFlightRef = useRef(false)

  const load = useCallback(async () => {
    if (inFlightRef.current) return
    inFlightRef.current = true
    setLoading(true)
    setError('')
    try {
      setData(await api.get<ServerStatus>('/api/admin/server-status'))
      setUpdatedAt(Date.now())
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load server status')
    } finally {
      inFlightRef.current = false
      loadedAtRef.current = Date.now()
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  useEffect(() => {
    const tick = setInterval(() => {
      // A hidden tab polls nothing; it refreshes on the first visible tick.
      if (document.visibilityState !== 'visible') return
      setNow(Date.now())
      if (Date.now() - loadedAtRef.current >= REFRESH_MS) void load()
    }, 1000)
    return () => clearInterval(tick)
  }, [load])

  const disk = data?.disk
  const cpu = data?.cpu
  const memory = data?.memory
  const process = data?.process
  const database = data?.database
  const sidecar = data?.sidecar
  const activity = data?.activity

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold">Server Status</h1>
          <p className="text-sm text-text-secondary">
            Live snapshot of this container — disk, CPU, memory, process,
            database and AI sidecar. Admin only, read-only.
          </p>
          {updatedAt !== null && (
            <p className="text-xs text-text-tertiary">
              updated{' '}
              {now - updatedAt < 1000
                ? 'just now'
                : formatRelativeTime(new Date(updatedAt).toISOString())}
            </p>
          )}
        </div>
        <Button variant="secondary" size="sm" onClick={() => void load()} disabled={loading}>
          <RefreshCw className={`mr-1 h-4 w-4 ${loading ? 'animate-spin' : ''}`} /> Refresh
        </Button>
      </div>

      {error && (
        <p role="alert" className="text-sm text-destructive">
          {error}
        </p>
      )}

      {data === null && loading && (
        <div className="flex items-center gap-2 text-text-tertiary">
          <RefreshCw className="h-4 w-4 animate-spin" /> Loading server status…
        </div>
      )}

      {data !== null && (
        <>
          {data.warnings.map((warning) => (
            <WarningBanner key={warning.code} warning={warning} />
          ))}

          <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
            <StatusCard title="Disk" icon={<HardDrive className="h-4 w-4" />}>
              {disk?.available ? (
                <div className="space-y-3">
                  <dl>
                    <Field label="Used">
                      {percentValue(
                        disk.used_ratio == null ? null : disk.used_ratio * 100,
                      )}
                    </Field>
                    <Field label="Space">
                      {bytesValue(disk.used_bytes)} of {bytesValue(disk.capacity_bytes)}
                    </Field>
                  </dl>
                  <Table>
                    <TableBody>
                      <TableRow>
                        <TableCell className="py-1 pl-0 text-xs text-text-tertiary">
                          Database
                        </TableCell>
                        <TableCell className="py-1 pr-0 text-right font-mono text-sm">
                          {bytesValue(disk.database_bytes)}
                        </TableCell>
                      </TableRow>
                      <TableRow>
                        <TableCell className="py-1 pl-0 text-xs text-text-tertiary">
                          Transcripts
                        </TableCell>
                        <TableCell className="py-1 pr-0 text-right font-mono text-sm">
                          {bytesValue(disk.transcripts_bytes)}
                        </TableCell>
                      </TableRow>
                      <TableRow>
                        <TableCell className="py-1 pl-0 text-xs text-text-tertiary">
                          Logs
                        </TableCell>
                        <TableCell className="py-1 pr-0 text-right font-mono text-sm">
                          {bytesValue(disk.logs_bytes)}
                        </TableCell>
                      </TableRow>
                    </TableBody>
                  </Table>
                  {disk.filesystem_note && (
                    <p className="text-xs text-text-tertiary">{disk.filesystem_note}</p>
                  )}
                </div>
              ) : (
                <NotVisible reason={disk?.reason} />
              )}
            </StatusCard>

            <StatusCard title="CPU" icon={<Cpu className="h-4 w-4" />}>
              {cpu?.available ? (
                <dl>
                  <Field label="Process">{percentValue(cpu.process_percent)}</Field>
                  <Field label="CPU quota">
                    {cpu.quota_cores != null ? (
                      `${cpu.quota_cores} cores`
                    ) : cpu.quota_state === 'unlimited' ? (
                      <span className="text-text-tertiary">no quota — unlimited</span>
                    ) : (
                      <NotVisible reason="cpu.max not readable" />
                    )}
                  </Field>
                  <Field label="Period">
                    {cpu.period_us == null ? (
                      <NotVisible />
                    ) : (
                      `${cpu.period_us / 1000} ms`
                    )}
                  </Field>
                </dl>
              ) : (
                <NotVisible reason={cpu?.reason} />
              )}
            </StatusCard>

            <StatusCard title="Memory" icon={<MemoryStick className="h-4 w-4" />}>
              {memory?.available ? (
                <dl>
                  <Field label="Process RSS">{bytesValue(memory.rss_bytes)}</Field>
                  <Field label="Container usage">
                    {bytesValue(memory.current_bytes)} of{' '}
                    {memory.limit_state === 'unlimited'
                      ? 'no limit set'
                      : memory.limit_state === 'unknown'
                        ? NOT_VISIBLE
                        : bytesValue(memory.limit_bytes)}
                  </Field>
                  <Field label="Working set">{bytesValue(memory.working_set_bytes)}</Field>
                  <Field label="Of limit">{percentValue(memory.percent_of_limit)}</Field>
                </dl>
              ) : (
                <NotVisible reason={memory?.reason} />
              )}
            </StatusCard>

            <StatusCard title="Process" icon={<Server className="h-4 w-4" />}>
              {process && (
                <dl>
                  <Field label="Version">{process.version}</Field>
                  <Field label="Git SHA">
                    {process.git_sha || <NotVisible />}
                  </Field>
                  <Field label="Uptime">{formatUptime(process.uptime_seconds)}</Field>
                  <Field label="PID">{process.pid}</Field>
                  <Field label="Restarts">
                    {process.restart_count == null ? <NotVisible /> : process.restart_count}
                  </Field>
                  <Field label="Loop lag">
                    {process.loop_lag_ms == null ? (
                      <span className="text-text-tertiary">no sample yet</span>
                    ) : (
                      `${process.loop_lag_ms.toFixed(1)} ms`
                    )}
                  </Field>
                </dl>
              )}
            </StatusCard>

            <StatusCard title="Database" icon={<Database className="h-4 w-4" />}>
              {database?.available ? (
                <dl>
                  <Field label="Size">{bytesValue(database.size_bytes)}</Field>
                  <Field label="Results">{countValue(database.results, 'not counted')}</Field>
                  <Field label="Failures">{countValue(database.failures, 'not counted')}</Field>
                  <Field label="Test entries">
                    {countValue(database.test_entries, 'not counted')}
                  </Field>
                  <Field label="Compression ratio">
                    {database.compression_ratio == null ? (
                      <span className="text-text-tertiary">—</span>
                    ) : (
                      `${database.compression_ratio.toFixed(2)}×`
                    )}
                  </Field>
                </dl>
              ) : (
                <NotVisible reason={database?.reason} />
              )}
            </StatusCard>

            <StatusCard title="AI sidecar" icon={<Bot className="h-4 w-4" />}>
              {sidecar && (
                <div className="space-y-2">
                  <Badge variant={sidecar.reachable ? 'success' : 'destructive'}>
                    {sidecar.reachable ? 'Reachable' : 'Unreachable'}
                  </Badge>
                  {sidecar.reachable ? (
                    <dl>
                      <Field label="Models">
                        {countValue(sidecar.models ?? undefined, 'catalog not listed')}
                      </Field>
                      <Field label="Free models">
                        {countValue(sidecar.free_models ?? undefined, 'not counted')}
                      </Field>
                    </dl>
                  ) : (
                    <NotVisible reason={sidecar.reason} />
                  )}
                </div>
              )}
            </StatusCard>

            <StatusCard title="Activity" icon={<Activity className="h-4 w-4" />}>
              {activity && (
                <div className="space-y-2">
                  <dl>
                    <Field label="Analyses running">
                      {countValue(activity.analyses_running, NOT_VISIBLE)}
                    </Field>
                    <Field label="Chat turns in flight">
                      {countValue(activity.chat_turns_in_flight, NOT_VISIBLE)}
                    </Field>
                    <Field label="SSE clients">
                      {countValue(activity.sse_clients, NOT_VISIBLE)}
                    </Field>
                  </dl>
                  {activity.analyses && activity.analyses.length > 0 && (
                    <ul className="space-y-1 text-xs text-text-secondary">
                      {activity.analyses.map((run) => (
                        <li key={run.job_id} className="flex justify-between gap-2">
                          <span className="truncate">
                            {run.job_name || run.job_id} #{run.build_number}
                          </span>
                          <span className="shrink-0 font-mono text-text-tertiary">
                            {run.elapsed_seconds == null
                              ? NOT_VISIBLE
                              : formatUptime(run.elapsed_seconds)}
                          </span>
                        </li>
                      ))}
                    </ul>
                  )}
                </div>
              )}
            </StatusCard>
          </div>

          <p className="flex items-center gap-1.5 text-xs text-text-tertiary">
            <Timer className="h-3 w-3" /> Refreshes every 30 s while this tab is visible.
          </p>
        </>
      )}
    </div>
  )
}
