import { useEffect, useRef, useState, useCallback } from 'react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { Trash2, ArrowDownToLine, Pause, Play, AlertCircle } from 'lucide-react'
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from '@/components/ui/tooltip'

/** Strip ANSI escape codes (color, bold, reset, etc.) from a string. */
function stripAnsi(text: string): string {
  // eslint-disable-next-line no-control-regex
  return text.replace(/\x1b\[[0-9;]*m/g, '')
}

const LEVEL_COLORS: Record<string, string> = {
  ERROR: 'text-red-400',
  CRITICAL: 'text-red-500 font-bold',
  WARNING: 'text-amber-400',
  INFO: 'text-emerald-400',
  DEBUG: 'text-zinc-500',
}

function getLineColor(line: string): string {
  for (const [level, color] of Object.entries(LEVEL_COLORS)) {
    if (line.includes(level)) return color
  }
  return 'text-zinc-300'
}

const INITIAL_LINES_OPTIONS = ['100', '500', '1000', '5000', '10000']

export function LogsPage() {
  const [lines, setLines] = useState<string[]>([])
  const [autoScroll, setAutoScroll] = useState(true)
  const [filter, setFilter] = useState('')
  const [levelFilter, setLevelFilter] = useState('all')
  const [initialLines, setInitialLines] = useState('500')
  const [connected, setConnected] = useState(false)
  const [error, setError] = useState('')
  const containerRef = useRef<HTMLDivElement>(null)
  const autoScrollRef = useRef(autoScroll)

  useEffect(() => { autoScrollRef.current = autoScroll }, [autoScroll])

  // Hide parent main overflow to prevent double scrollbar
  useEffect(() => {
    const main = document.querySelector('main')
    if (!main) return
    const prev = main.style.overflow
    main.style.overflow = 'hidden'
    return () => { main.style.overflow = prev }
  }, [])

  const scrollToBottom = useCallback(() => {
    if (containerRef.current) {
      containerRef.current.scrollTop = containerRef.current.scrollHeight
    }
  }, [])

  useEffect(() => {
    const params = new URLSearchParams()
    params.set('lines', initialLines)
    if (levelFilter && levelFilter !== 'all') params.set('level', levelFilter)

    setError('')
    const es = new EventSource(`/api/admin/logs/stream?${params}`)

    es.addEventListener('log', (e) => {
      const clean = stripAnsi(e.data)
      setLines(prev => {
        const next = [...prev, clean]
        return next.length > 10000 ? next.slice(-10000) : next
      })
      setError('')
    })

    es.addEventListener('error', (e) => {
      if (e instanceof MessageEvent && e.data) {
        setError(e.data)
      }
    })

    es.onopen = () => {
      setConnected(true)
      setError('')
    }
    es.onerror = () => {
      setConnected(false)
      setError('Connection lost — retrying...')
    }

    return () => es.close()
  }, [levelFilter, initialLines])

  // Auto-scroll effect
  useEffect(() => {
    if (autoScrollRef.current) {
      requestAnimationFrame(scrollToBottom)
    }
  }, [lines, scrollToBottom])

  const filteredLines = filter
    ? lines.filter(l => l.toLowerCase().includes(filter.toLowerCase()))
    : lines

  return (
    <TooltipProvider delayDuration={200}>
    <div className="flex flex-col -mx-4 -my-6 sm:-mx-6 lg:-mx-8" style={{ height: 'calc(100vh - 56px)' }}>
      {/* Fixed toolbar */}
      <div className="shrink-0 flex items-center gap-2 p-3 border-b border-border bg-surface-secondary">
        <Tooltip>
          <TooltipTrigger asChild>
            <div className={`h-2 w-2 rounded-full shrink-0 ${connected ? 'bg-emerald-500' : 'bg-red-500'}`} />
          </TooltipTrigger>
          <TooltipContent>{connected ? 'Connected' : 'Disconnected'}</TooltipContent>
        </Tooltip>
        <span className="text-xs text-text-secondary whitespace-nowrap">{filteredLines.length} lines</span>

        <Select value={levelFilter} onValueChange={(v) => { setLevelFilter(v); setLines([]) }}>
          <SelectTrigger className="h-8 w-32 text-xs">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">All levels</SelectItem>
            <SelectItem value="DEBUG">DEBUG</SelectItem>
            <SelectItem value="INFO">INFO</SelectItem>
            <SelectItem value="WARNING">WARNING</SelectItem>
            <SelectItem value="ERROR">ERROR</SelectItem>
          </SelectContent>
        </Select>

        <Select value={initialLines} onValueChange={(v) => { setInitialLines(v); setLines([]) }}>
          <SelectTrigger className="h-8 w-28 text-xs">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {INITIAL_LINES_OPTIONS.map(n => (
              <SelectItem key={n} value={n}>Last {n}</SelectItem>
            ))}
          </SelectContent>
        </Select>

        <Input
          placeholder="Filter logs..."
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
          className="h-8 w-48 text-xs"
        />

        <div className="flex-1" />

        <Tooltip>
          <TooltipTrigger asChild>
            <Button variant="ghost" size="icon" className="h-8 w-8"
              onClick={() => setAutoScroll(!autoScroll)}>
              {autoScroll ? <Pause className="h-4 w-4" /> : <Play className="h-4 w-4" />}
            </Button>
          </TooltipTrigger>
          <TooltipContent>{autoScroll ? 'Pause auto-scroll' : 'Resume auto-scroll'}</TooltipContent>
        </Tooltip>
        <Tooltip>
          <TooltipTrigger asChild>
            <Button variant="ghost" size="icon" className="h-8 w-8"
              onClick={scrollToBottom}>
              <ArrowDownToLine className="h-4 w-4" />
            </Button>
          </TooltipTrigger>
          <TooltipContent>Scroll to bottom</TooltipContent>
        </Tooltip>
        <Tooltip>
          <TooltipTrigger asChild>
            <Button variant="ghost" size="icon" className="h-8 w-8"
              onClick={() => setLines([])}>
              <Trash2 className="h-4 w-4" />
            </Button>
          </TooltipTrigger>
          <TooltipContent>Clear logs</TooltipContent>
        </Tooltip>
      </div>

      {/* Error banner */}
      {error && (
        <div className="shrink-0 flex items-center gap-2 px-3 py-2 bg-red-500/10 border-b border-red-500/20 text-red-400 text-xs">
          <AlertCircle className="h-3.5 w-3.5 shrink-0" />
          {error}
        </div>
      )}

      {/* Log output — scrollable */}
      <div ref={containerRef}
        className="flex-1 h-0 overflow-y-auto bg-zinc-950 p-3 font-mono text-xs leading-5">
        {filteredLines.length === 0 && !error && (
          <div className="text-zinc-500 italic">Waiting for log entries...</div>
        )}
        {filteredLines.map((line, i) => (
          <div key={i} className={getLineColor(line)}>{line}</div>
        ))}
      </div>
    </div>
    </TooltipProvider>
  )
}
