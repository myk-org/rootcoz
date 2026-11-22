import { useState, useEffect, useRef, useCallback, useMemo, type FormEvent, type KeyboardEvent } from 'react'
import { api, ApiError, extractApiDetail } from '@/lib/api'
import { useSSE } from '@/lib/useSSE'
import { Button } from '@/components/ui/button'
import { AnalysisModelNotice, AnalysisProviderSelect, AnalysisModelSelect } from '@/components/shared/AnalysisAiPicker'
import { isAnalysisAiAvailable } from '@/lib/analysisAi'
import { useProviderCatalog } from '@/lib/useProviderOptions'
import { CredentialAccessNotice } from '@/components/shared/CredentialAccessNotice'
import { useAuth } from '@/lib/auth'
import { useAiSelection } from '@/lib/useAiSelection'
import { formatDuration, parseApiTimestamp, INVALID_DATE_FALLBACK } from '@/lib/utils'
import { Tooltip, TooltipTrigger, TooltipContent } from '@/components/ui/tooltip'
import { TooltipProvider } from '@/components/ui/tooltip'
import { Textarea } from '@/components/ui/textarea'
import { AttachmentPicker } from '@/components/shared/AttachmentPicker'
import { LinkedText } from '@/components/shared/LinkedText'
import { ChatMarkdown } from '@/components/shared/ChatMarkdown'
import type { RepoUrl } from '@/lib/autoLink'
import { Send, Loader2, Bot, User, Copy, Check, Info } from 'lucide-react'

const EMPTY_REPO_URLS: RepoUrl[] = []

/** Roles allowed to abort a turn — mirrors the server's `_require_reviewer`. */
const ABORT_ROLES = ['reviewer', 'operator', 'admin']

const POLL_INITIAL_DELAY_MS = 2000
const POLL_RECOVERY_DELAY_MS = 500
const POLL_INTERVAL_MS = 3000
const POLL_MAX_BACKOFF_MS = 10000

const GATEWAY_TURN_SYNC_NOTICE =
  'Connection interrupted while waiting for the reply — reconnecting automatically. Your turn is still running on the server; no need to resend.'
const GATEWAY_SEND_RECOVERY_NOTICE =
  'Gateway timed out while sending — checking whether your message is already running. No need to resend.'
const GATEWAY_SEND_LOST_ERROR =
  'The gateway timed out before your message reached the server. Your text has been restored — try sending again.'
const GATEWAY_HISTORY_ERROR =
  'Could not reach the server to load chat history (gateway timeout). Click Retry history to try again.'
const GATEWAY_ACTION_ERROR =
  'The server gateway timed out while handling that request. Please try again.'

/** True for reverse-proxy / gateway 5xx responses (e.g. 502, 503, 504). */
function isTransientGatewayError(err: unknown): boolean {
  return err instanceof ApiError && err.status >= 500 && err.status < 600
}

/** True for transient gateway 5xx or dropped-connection transport errors. */
function isTransientTransportError(err: unknown): boolean {
  return isTransientGatewayError(err) || !(err instanceof ApiError)
}

/** Log transport/gateway failures with provider, model, and elapsed time for diagnostics. */
function logChatTransportError(
  action: string,
  err: unknown,
  choice: SessionChoice | null,
  sinceMs: number | null,
) {
  const detail = err instanceof ApiError
    ? `HTTP ${err.status} ${err.statusText}`.trim()
    : err instanceof Error ? err.message : 'unknown error'
  const elapsed = sinceMs !== null ? `${Math.max(0, Math.round((Date.now() - sinceMs) / 1000))}s` : 'n/a'
  const pair = choice ? `${choice.provider}/${choice.model}` : 'none'
  console.warn(`[ChatUI] ${action} failed (${detail}, pair=${pair}, elapsed=${elapsed})`)
}

/** Replace raw "API error 504: Gateway Time-out" with actionable user-facing copy. */
function formatChatError(err: unknown, fallback: string, gatewayCopy: string): string {
  const detail = extractApiDetail(err)
  if (detail) return detail
  if (isTransientGatewayError(err)) return gatewayCopy
  return err instanceof Error ? err.message : fallback
}

const INIT_STEPS = [
  'Initializing workspace and cloning repositories...',
  'Loading chat history...',
  'Ready',
] as const

/** Workspace-preparation phases reported by POST /init, in order. */
const PREP_STEPS = [
  { phase: 'preparing', label: 'Preparing workspace' },
  { phase: 'cloning', label: 'Cloning repositories' },
  { phase: 'fetching_build_data', label: 'Fetching CI build data' },
  { phase: 'starting_session', label: 'Starting AI session' },
] as const

export interface ChatMessage {
  id: number
  job_id: string
  /** `system` rows are written by the server (an issue preview), never by a model. */
  role: 'user' | 'assistant' | 'system'
  content: string
  username: string
  ai_provider: string
  ai_model: string
  status: string
  created_at: string
}

type ChatHistory = { messages: ChatMessage[]; total: number; active_session_version: string; active_session?: { ai_provider: string; ai_model: string; credential_source: string } | null; preparing?: { phase: string; detail: string } | null }
type SessionChoice = { provider: string; model: string; credentialSource: string }

interface ChatUIProps {
  /** API base path — e.g. '/api/chat/job123' or '/api/admin/chat' */
  apiBasePath: string
  /** SSE topic for the multiplexed stream (e.g. 'chat:job123' or 'admin-chat') */
  sseTopic: string
  /** Header content rendered above the chat */
  header: React.ReactNode
  /** Initial AI provider */
  defaultProvider?: string
  /** Initial AI model */
  defaultModel?: string
  /** Empty state message */
  emptyMessage?: string
  /** Empty state subtitle */
  emptySubtitle?: string
}

function StepIndicator({ label, done, active }: { label: string; done: boolean; active: boolean }) {
  return (
    <div className={`flex items-center gap-2 ${done ? 'text-signal-green' : active ? 'text-accent-blue' : 'text-text-tertiary'}`}>
      {done ? (
        <Check className="h-3 w-3" />
      ) : active ? (
        <Loader2 className="h-3 w-3 animate-spin" />
      ) : (
        <div className="h-3 w-3 rounded-full border border-current opacity-30" />
      )}
      <span>{label}</span>
    </div>
  )
}

/** Busy bubble shared by the transient `Sending...` row and the persistent pending row. */
function ChatBusyRow({ label, since, canStop, onStop, text = '' }: { label: string; since?: Date | null; canStop: boolean; onStop: () => void; text?: string }) {
  // Ticks only while this bubble is mounted, i.e. only while something is busy.
  const [now, setNow] = useState(() => Date.now())
  const sinceMs = since ? since.getTime() : null
  useEffect(() => {
    if (sinceMs === null) return
    const id = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(id)
  }, [sinceMs])
  // formatDuration falls back to a dash when the browser clock is behind the
  // server; render the bubble without a number rather than a nonsense one.
  const elapsed = sinceMs === null ? '' : formatDuration(new Date(sinceMs), new Date(now))
  const showElapsed = elapsed !== '' && elapsed !== INVALID_DATE_FALLBACK
  return (
    <div className="flex gap-3 justify-start">
      <div className="shrink-0 mt-1">
        <div className="h-7 w-7 rounded-full bg-accent-blue/15 flex items-center justify-center">
          <Bot className="h-4 w-4 text-accent-blue" />
        </div>
      </div>
      <div className="max-w-[75%] rounded-lg px-4 py-3 text-sm bg-surface-elevated text-text-secondary">
        <div className="flex items-center gap-2 text-text-tertiary">
          <Loader2 className="h-4 w-4 animate-spin" />
          {/* The ticking label is hidden from assistive tech — a live region that
              re-announces "Thinking... 12s" every second is noise. Transitions
              are announced by the hidden role="status" region instead. */}
          <span className="text-sm" aria-hidden="true">{showElapsed ? `${label} ${elapsed}` : label}</span>
          {canStop && (
            <Button
              type="button"
              variant="outline"
              size="sm"
              className="h-6 px-2 text-xs text-signal-red border-signal-red/30 hover:bg-signal-red/10 ml-2"
              onClick={onStop}
            >
              Stop
            </Button>
          )}
        </div>
        {/* Provisional answer: display only — never in a live region (the hidden
            role="status" below announces state transitions, not tokens). The
            stored reply replaces it when the pending row goes away. */}
        {text && (
          <div className="mt-2">
            <ChatMarkdown content={text} />
          </div>
        )}
      </div>
    </div>
  )
}

export function ChatUI({
  apiBasePath,
  sseTopic,
  header,
  defaultProvider = '',
  defaultModel = '',
  emptyMessage = 'Start a conversation',
  emptySubtitle = '',
}: ChatUIProps) {
  const [messages, setMessages] = useState<ChatMessage[]>([])
  const [input, setInput] = useState('')
  const [error, setError] = useState('')
  const [transientNotice, setTransientNotice] = useState('')
  const [recoveringSend, setRecoveringSend] = useState(false)
  const [sessionStarted, setSessionStarted] = useState(false)
  const [sessionChoice, setSessionChoice] = useState<SessionChoice | null>(null)
  const [starting, setStarting] = useState(false)
  // A 202 means the server prepares the workspace; `starting` then stays true
  // until the server reports a session, a failed row or no preparation at all.
  const prepRunningRef = useRef(false)
  // Counts the history answers in which the client saw a preparation end (a
  // session or a failed row, with no phase). A 202 whose POST began before such
  // an answer describes a preparation already over, so it must not take Start.
  const settleMarkerRef = useRef(0)
  const [initStepIndex, setInitStepIndex] = useState(0)
  const [prepPhase, setPrepPhase] = useState<{ phase: string; detail: string } | null>(null)
  const [clearing, setClearing] = useState(false)
  const [loadingHistory, setLoadingHistory] = useState(true)
  const [historyFailed, setHistoryFailed] = useState(false)
  const [historyRetry, setHistoryRetry] = useState(0)

  // Remembered pair; the props only seed the very first use. Chat has no
  // "Server default" entry: an empty pair cannot start a session, so a cleared
  // entry elsewhere in the app falls back to the analysed pair instead.
  const { aiProvider, aiModel, setAiPair } = useAiSelection(defaultProvider, defaultModel, { respectCleared: false })
  const { canUseServerProviders, canUseFreeProviders, role } = useAuth()
  const { providers, providerStatus } = useProviderCatalog()

  const [copiedMsgId, setCopiedMsgId] = useState<number | null>(null)
  const [copiedAll, setCopiedAll] = useState(false)

  const messagesEndRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLTextAreaElement>(null)
  const pollTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const pollGenerationRef = useRef(0)
  const historyGenerationRef = useRef(0)
  const clearingRef = useRef(false)
  const preflightRef = useRef(false)
  const sendInFlightRef = useRef(false)
  const recoveringSendRef = useRef(false)
  const pendingSendRef = useRef<{ content: string; prevMaxId: number; startedAt: number } | null>(null)
  const [sending, setSending] = useState(false)
  const mountedRef = useRef(true)
  const startInFlightRef = useRef(false)
  const confirmedInitRef = useRef(false)
  // Opaque version of the session currently rendered; a mismatch against a fresh GET
  // means another tab replaced it, so New Session must not delete the replacement.
  const sessionVersionRef = useRef<string | null>(null)

  useEffect(() => {
    mountedRef.current = true
    return () => { mountedRef.current = false }
  }, [])

  const pendingAssistantMsg = useMemo(
    () => messages.find(m => m.role === 'assistant' && m.status === 'pending') ?? null,
    [messages],
  )
  const hasPending = pendingAssistantMsg !== null
  const sendBusy = sending || recoveringSend || hasPending
  const messagesRef = useRef<ChatMessage[]>(messages)
  messagesRef.current = messages
  const pendingAssistantRef = useRef<ChatMessage | null>(pendingAssistantMsg)
  pendingAssistantRef.current = pendingAssistantMsg
  const sessionChoiceRef = useRef<SessionChoice | null>(sessionChoice)
  sessionChoiceRef.current = sessionChoice

  // Provisional answer text per assistant message id (fragments only, never persisted).
  const [streamTexts, setStreamTexts] = useState<Record<number, string>>({})

  // One delta stream per busy stretch: opened when a turn starts (before the
  // assistant id is known), closed when nothing is busy. Frames are buffered per
  // id and flushed at most once per animation frame — a long reply must not be one
  // React render per token.
  useEffect(() => {
    if (!sendBusy) return
    const buffer = new Map<number, string>()
    let frame = 0
    const flush = () => {
      frame = 0
      const added = new Map(buffer)
      buffer.clear()
      if (added.size === 0) return
      setStreamTexts(prev => {
        const next = { ...prev }
        for (const [id, text] of added) next[id] = (next[id] ?? '') + text
        return next
      })
    }
    const source = new EventSource(`${apiBasePath}/stream`)
    const onDelta = (event: MessageEvent) => {
      let frameData: { id?: number; delta?: string; snapshot?: string }
      try {
        frameData = JSON.parse(String(event.data))
      } catch {
        return
      }
      if (typeof frameData.id !== 'number') return
      if (typeof frameData.snapshot === 'string') {
        // Catch-up frame for a stream that subscribed mid-turn: it replaces the
        // fragments, so a reconnect can never render the same text twice.
        const id = frameData.id
        const snapshot = frameData.snapshot
        buffer.delete(id)
        setStreamTexts(prev => ({ ...prev, [id]: snapshot }))
        return
      }
      if (typeof frameData.delta !== 'string') return
      buffer.set(frameData.id, (buffer.get(frameData.id) ?? '') + frameData.delta)
      if (!frame) frame = requestAnimationFrame(flush)
    }
    source.addEventListener('chat-delta', onDelta)
    return () => {
      source.removeEventListener('chat-delta', onDelta)
      source.close()
      if (frame) cancelAnimationFrame(frame)
    }
  }, [sendBusy, apiBasePath])

  // Drop provisional text as soon as its row stops being pending (bounded memory).
  useEffect(() => {
    setStreamTexts(prev => {
      const pendingIds = new Set(messages.filter(m => m.status === 'pending').map(m => m.id))
      const kept = Object.keys(prev).filter(id => pendingIds.has(Number(id)))
      if (kept.length === Object.keys(prev).length) return prev
      return Object.fromEntries(kept.map(id => [id, prev[Number(id)]]))
    })
  }, [messages])
  // Mirrors the server's `_require_reviewer` on abort_chat, which never checks that a
  // session exists. Gating on sessionStarted would hide Stop whenever a pending turn
  // outlives the session (credential or server grant revoked), wedging the composer.
  // A stale role still fails safely: the 403 surfaces and the bubble keeps ticking.
  const canStop = ABORT_ROLES.includes(role)

  const historyIsCurrent = useCallback((generation: number) =>
    mountedRef.current && !clearingRef.current && historyGenerationRef.current === generation, [])
  // Server-driven prep progress: `preparing` is what the server reports, so a
  // reload mid-preparation shows the same list as the click that started it.
  const prepActiveIndex = useMemo(() => {
    const found = PREP_STEPS.findIndex(s => s.phase === prepPhase?.phase)
    return found === -1 ? 0 : found
  }, [prepPhase])
  const prepStatus = prepPhase
    ? `${PREP_STEPS[prepActiveIndex]?.label ?? INIT_STEPS[0]}${prepPhase.detail ? `: ${prepPhase.detail}` : ''}...`
    : INIT_STEPS[initStepIndex]
  const prepVisible = !sessionStarted && (starting || Boolean(prepPhase))
  const validPair = isAnalysisAiAvailable(providers, providerStatus, aiProvider, aiModel, false, canUseServerProviders, canUseFreeProviders)
  const canSend = Boolean(sessionStarted && !clearing && input.trim() && !sendBusy)
  const sendBlockedReason = clearing ? 'Clearing chat session'
    : !sessionStarted
    ? 'Start Chat before sending'
    : recoveringSend
      ? 'Checking whether your previous message is already running'
    : sendBusy
      ? 'Wait for the current reply to finish'
      : !input.trim()
        ? 'Type a message'
        : undefined

  // Fetch messages with pagination (last 200); session metadata belongs to the first page.
  // Callers apply `preparing` themselves, after their generation check — an
  // out-of-order response must not rewind the preparation phase.
  const fetchMessages = useCallback(async (): Promise<{ history: ChatHistory; generation: number }> => {
    const generation = historyGenerationRef.current
    const history = await api.get<ChatHistory>(apiBasePath)
    if (history.total > 200) {
      const lastPage = await api.get<ChatHistory>(
        `${apiBasePath}?offset=${Math.max(history.total - 200, 0)}`
      )
      return { history: { ...history, messages: lastPage.messages }, generation }
    }
    return { history, generation }
  }, [apiBasePath])

  // The server is the only one that knows when a preparation ended: a session, a
  // failed row, or no `preparing` at all.
  const settlePreparation = useCallback((history: ChatHistory) => {
    const failed = history.messages.some(m => m.role === 'assistant' && m.status === 'failed')
    if (history.active_session || failed || !history.preparing) {
      // Only a positive end product marks a settle: a merely empty history can
      // predate the server registering the preparation, and must not block a
      // 202 that is its own first report.
      if (!history.preparing && (history.active_session || failed)) settleMarkerRef.current++
      if (!prepRunningRef.current) return
      prepRunningRef.current = false
      setStarting(false)
    }
  }, [])

  const applySession = useCallback((active: ChatHistory['active_session'], version?: string) => {
    sessionVersionRef.current = active ? version ?? null : null
    if (!active) {
      confirmedInitRef.current = false
      setSessionChoice(null)
      setSessionStarted(false)
    } else {
      setSessionChoice({ provider: active.ai_provider, model: active.ai_model, credentialSource: active.credential_source })
      setSessionStarted(true)
    }
  }, [])

  // Cancel active polling
  const cancelPoll = useCallback(() => {
    pollGenerationRef.current++
    if (pollTimerRef.current) {
      clearTimeout(pollTimerRef.current)
      pollTimerRef.current = null
    }
  }, [])

  const pendingSinceMs = useCallback((): number | null => {
    const pending = pendingAssistantRef.current
    if (pending) return parseApiTimestamp(pending.created_at)?.getTime() ?? null
    return pendingSendRef.current?.startedAt ?? null
  }, [])

  const startPollRef = useRef<(assistantMsgId: number | null, initialDelayMs?: number) => void>(() => {})

  /** Single authoritative state updater for every successful history GET. */
  const applyHistoryResponse = useCallback((
    history: ChatHistory,
    options: { updateSession: 'always' | 'if-available'; ensurePoll?: boolean },
  ) => {
    const msgs = history.messages
    setMessages(msgs)
    setPrepPhase(history.preparing ?? null)
    settlePreparation(history)
    setHistoryFailed(false)
    setTransientNotice('')
    if (options.updateSession === 'always') {
      applySession(history.active_session, history.active_session_version)
    } else if (history.active_session !== undefined && (history.active_session || !confirmedInitRef.current)) {
      applySession(history.active_session, history.active_session_version)
    }

    // Reconcile a POST that hit a gateway 5xx: check whether the server persisted
    // the turn before the gateway dropped the response.
    if (recoveringSendRef.current && pendingSendRef.current) {
      const { content, prevMaxId } = pendingSendRef.current
      const persisted = msgs.some(m => m.id > prevMaxId)
      pendingSendRef.current = null
      recoveringSendRef.current = false
      setRecoveringSend(false)
      if (persisted) {
        setError('')
      } else {
        setInput(curr => curr || content)
        setError(GATEWAY_SEND_LOST_ERROR)
      }
    }

    if (options.ensurePoll === false) return
    const pending = msgs.find(m => m.role === 'assistant' && m.status === 'pending')
    if (pending) {
      if (!pollTimerRef.current) startPollRef.current(pending.id)
    } else if (!recoveringSendRef.current) {
      cancelPoll()
    }
  }, [applySession, cancelPoll, settlePreparation])

  // Polling fallback: fetch messages until the pending turn resolves or send-recovery settles.
  // Never gives up silently after an arbitrary wall-clock cap: slow turns can exceed 5 minutes,
  // and unmount / New Session / completion / abort all cancel this timer explicitly.
  const startPollForResponse = useCallback((
    assistantMsgId: number | null,
    initialDelayMs: number = POLL_INITIAL_DELAY_MS,
  ) => {
    cancelPoll()
    const generation = pollGenerationRef.current
    let consecutiveFailures = 0
    console.debug('[ChatUI] Poll started — generation', generation, 'waiting for assistant msg', assistantMsgId)

    const poll = () => {
      if (pollGenerationRef.current !== generation) return
      pollTimerRef.current = null
      fetchMessages()
        .then(({ history, generation: historyGeneration }) => {
          if (pollGenerationRef.current !== generation || !historyIsCurrent(historyGeneration)) return
          consecutiveFailures = 0
          setError(prev => (prev === GATEWAY_HISTORY_ERROR ? '' : prev))
          applyHistoryResponse(history, { updateSession: 'if-available', ensurePoll: false })
          const msgs = history.messages
          const stillPending = assistantMsgId !== null
            ? msgs.some(m => m.id === assistantMsgId && m.status === 'pending')
            : msgs.some(m => m.role === 'assistant' && m.status === 'pending')
          if (!stillPending && !recoveringSendRef.current) {
            console.debug('[ChatUI] Poll completed — assistant response received')
            pollTimerRef.current = null
          } else {
            pollTimerRef.current = setTimeout(poll, POLL_INTERVAL_MS)
          }
        })
        .catch((err) => {
          if (pollGenerationRef.current !== generation || clearingRef.current || !mountedRef.current) return
          logChatTransportError('Chat poll', err, sessionChoiceRef.current, pendingSinceMs())
          if (!isTransientTransportError(err)) {
            pollTimerRef.current = null
            setHistoryFailed(true)
            setTransientNotice('')
            setError(formatChatError(err, 'Failed to sync chat', GATEWAY_HISTORY_ERROR))
            return
          }
          consecutiveFailures++
          setTransientNotice(
            recoveringSendRef.current ? GATEWAY_SEND_RECOVERY_NOTICE : GATEWAY_TURN_SYNC_NOTICE,
          )
          const delay = Math.min(POLL_INTERVAL_MS * consecutiveFailures, POLL_MAX_BACKOFF_MS)
          pollTimerRef.current = setTimeout(poll, delay)
        })
    }

    // Start after short delay to give SSE a chance first
    pollTimerRef.current = setTimeout(poll, initialDelayMs)
  }, [fetchMessages, cancelPoll, historyIsCurrent, applyHistoryResponse, pendingSinceMs])
  startPollRef.current = startPollForResponse

  // History is readable without creating a workspace or AI session.
  useEffect(() => {
    let ignore = false
    const generation = historyGenerationRef.current
    setLoadingHistory(true)
    setHistoryFailed(false)
    fetchMessages()
      .then(({ history: res }) => {
        if (ignore || !historyIsCurrent(generation)) return
        applyHistoryResponse(res, { updateSession: 'if-available' })
        console.info('[ChatUI] Chat history loaded, active session:', !!res.active_session)
      })
      .catch(err => {
        if (!ignore && historyIsCurrent(generation)) {
          logChatTransportError('Initial history load', err, sessionChoiceRef.current, null)
          setHistoryFailed(true)
          setError(formatChatError(err, 'Failed to load chat history', GATEWAY_HISTORY_ERROR))
        }
      })
      // Not generation-gated: a send or a sync can invalidate this fetch, and the
      // spinner must still clear once the initial load has settled.
      .finally(() => { if (!ignore && mountedRef.current) setLoadingHistory(false) })
    return () => { ignore = true }
  }, [fetchMessages, historyRetry, historyIsCurrent, applyHistoryResponse])

  const handleStart = async () => {
    if (startInFlightRef.current || confirmedInitRef.current || starting || clearing || loadingHistory || historyFailed || sessionStarted || !validPair) return
    startInFlightRef.current = true
    setStarting(true)
    setInitStepIndex(0)
    setPrepPhase(null)
    setError('')
    console.info('[ChatUI] Starting chat session')
    // A 202 hands the wait to `settlePreparation`: `starting` must survive this
    // function's `finally` or Start becomes clickable again mid-preparation.
    const startGeneration = historyGenerationRef.current
    const startSettleMarker = settleMarkerRef.current
    try {
      const result = await api.post<{
        ready: boolean
        session_id?: string
        preparing?: { phase: string; detail: string } | null
      }>(`${apiBasePath}/init`, {
        ai_provider: aiProvider, ai_model: aiModel,
      })
      if (!mountedRef.current) return
      if (!result.ready || !result.session_id) {
        // A 202 means the server is preparing the workspace: there is nothing to
        // report as unavailable, and the phase list is the progress.
        if (result.preparing) {
          // The 202 is the server saying a preparation is running, and it owns
          // `starting` before any generation check: a sync that raced this
          // request was answered before the server registered the preparation,
          // so its "no preparing" cannot be read as "already ended". But a sync
          // that reported the preparation's end after this POST began already
          // answered it — nothing re-syncs, so taking Start here would leave it
          // disabled until a reload. Only the phase text is generation-gated — a
          // newer one must not be rewound.
          if (settleMarkerRef.current === startSettleMarker) prepRunningRef.current = true
          if (historyGenerationRef.current === startGeneration) {
            setPrepPhase(result.preparing)
            setInitStepIndex(0)
          }
          console.info('[ChatUI] Chat preparation started in the background')
          return
        }
        throw new Error('Chat session unavailable. Clear chat and Start a new session.')
      }
      confirmedInitRef.current = true
      setSessionChoice({ provider: aiProvider, model: aiModel, credentialSource: '' })
      setSessionStarted(true)
      setInitStepIndex(1)
      console.info('[ChatUI] Chat session confirmed')
      try {
        const { history, generation } = await fetchMessages()
        if (!historyIsCurrent(generation)) return
        setMessages(history.messages)
        if (history.active_session) {
          sessionVersionRef.current = history.active_session_version
          // The server decides which source it used; pin the reported one.
          setSessionChoice((choice) => choice && { ...choice, credentialSource: history.active_session!.credential_source })
        }
        setPrepPhase(history.preparing ?? null)
        setInitStepIndex(2)
        console.info('[ChatUI] Chat history loaded after start')
      } catch (err) {
        if (!mountedRef.current || clearingRef.current) return
        logChatTransportError('Post-start history load', err, sessionChoiceRef.current, null)
        setHistoryFailed(true)
        setError(formatChatError(err, 'Failed to load chat history', GATEWAY_HISTORY_ERROR))
      }
    } catch (err) {
      if (!mountedRef.current) return
      logChatTransportError('Chat start', err, sessionChoiceRef.current, null)
      setError(formatChatError(err, 'Failed to start chat', GATEWAY_ACTION_ERROR))
    } finally {
      startInFlightRef.current = false
      // A handed-off preparation still owns the spinner: `settlePreparation` is
      // the only thing that ends one, and it clears this ref first.
      if (mountedRef.current && !prepRunningRef.current) setStarting(false)
    }
  }

  // Cleanup repos when leaving chat (keep sessions)
  useEffect(() => {
    return () => {
      // Fire-and-forget cleanup — don't await
      api.post(`${apiBasePath}/close`, {}).catch(() => {})
    }
  }, [apiBasePath])

  // Multiplexed SSE: listen for chat message updates (AI responses)
  const fetchMessagesRef = useRef(fetchMessages)
  fetchMessagesRef.current = fetchMessages
  const cancelPollRef = useRef(cancelPoll)
  cancelPollRef.current = cancelPoll
  // Stop the response poll on unmount — without this the leaked timer keeps
  // issuing history GETs for up to 5 minutes after leaving the chat page.
  useEffect(() => cancelPollRef.current, [])
  const historyIsCurrentRef = useRef(historyIsCurrent)
  historyIsCurrentRef.current = historyIsCurrent

  const syncHistory = useCallback(() => {
    if (clearingRef.current) {
      if (preflightRef.current) historyGenerationRef.current++
      return
    }
    const generation = ++historyGenerationRef.current
    fetchMessagesRef.current()
      .then(({ history }) => {
        if (!historyIsCurrentRef.current(generation)) return
        setError(prev => (prev === GATEWAY_HISTORY_ERROR ? '' : prev))
        applyHistoryResponse(history, { updateSession: 'always' })
        setLoadingHistory(false)
        console.info('[ChatUI] Chat synchronized, active session:', !!history.active_session)
      })
      .catch((err) => {
        if (!historyIsCurrentRef.current(generation)) return
        logChatTransportError('Chat sync', err, sessionChoiceRef.current, pendingSinceMs())
        setLoadingHistory(false)
        // A gateway 5xx while a turn is in flight is a transient failure of the
        // reporting path, not of the server-side turn: keep the pending state
        // alive and keep polling so the reply appears without a manual reload.
        const turnActive = pendingAssistantRef.current !== null || recoveringSendRef.current
        if (turnActive && isTransientTransportError(err)) {
          setTransientNotice(
            recoveringSendRef.current ? GATEWAY_SEND_RECOVERY_NOTICE : GATEWAY_TURN_SYNC_NOTICE,
          )
          if (!pollTimerRef.current) {
            startPollRef.current(pendingAssistantRef.current?.id ?? null, POLL_INTERVAL_MS)
          }
          return
        }
        setHistoryFailed(true)
        setError(formatChatError(err, 'Failed to sync chat', GATEWAY_HISTORY_ERROR))
      })
  }, [applyHistoryResponse, pendingSinceMs])

  const chatEvents = useMemo(() => ({
    'chat-changed': () => {
      console.debug('[ChatUI] SSE chat-changed received, syncing history')
      syncHistory()
    },
  }), [syncHistory])

  const chatSseOnReconnect = useCallback(() => {
    console.debug('[ChatUI] SSE reconnected, syncing history')
    syncHistory()
  }, [syncHistory])

  useSSE(sseTopic, chatEvents, { onReconnect: chatSseOnReconnect })

  // Auto-scroll — `sending` and `recoveringSend` are in the deps because transient
  // busy rows are rendered from those flags, not from `messages`.
  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages, sending, recoveringSend])

  const handleSend = useCallback(async (e?: FormEvent) => {
    e?.preventDefault()
    const trimmed = input.trim()
    if (!trimmed || !sessionStarted || clearing || sendBusy || sendInFlightRef.current) return
    sendInFlightRef.current = true
    const prevMaxId = messagesRef.current.reduce((max, m) => Math.max(max, m.id), 0)
    const sendStartedAt = Date.now()
    // A history fetch started before this send carries a snapshot without the
    // optimistic row; invalidate it so its late arrival cannot drop the row and
    // re-enable the composer mid-turn.
    historyGenerationRef.current++
    setSending(true)
    console.info('[ChatUI] Sending message')
    setError('')
    setTransientNotice('')
    setInput('')

    try {
      const res = await api.post<{
        user_message: { id: number; role: string; content: string; username: string; status: string }
        assistant_message_id: number
      }>(apiBasePath, {
        message: trimmed,
        ...(sessionChoice && { ai_provider: sessionChoice.provider, ai_model: sessionChoice.model }),
      })

      if (!mountedRef.current) return
      // Insert both rows now so the busy bubble is visible before any refetch lands.
      setMessages(prev => {
        const additions: ChatMessage[] = []
        // Dedupe by id on both rows: an SSE sync may have beaten the POST response,
        // and replacing a synced row would downgrade a finished reply to pending.
        if (!prev.some(m => m.id === res.user_message.id)) {
          additions.push({
            id: res.user_message.id,
            job_id: '',
            role: 'user',
            content: trimmed,
            username: res.user_message.username,
            ai_provider: '',
            ai_model: '',
            status: 'completed',
            created_at: new Date().toISOString(),
          })
        }
        if (!prev.some(m => m.id === res.assistant_message_id)) {
          additions.push({
            id: res.assistant_message_id,
            job_id: '',
            role: 'assistant',
            content: '',
            status: 'pending',
            created_at: new Date().toISOString(),
            ai_provider: sessionChoice?.provider ?? '',
            ai_model: sessionChoice?.model ?? '',
            username: res.user_message.username,
          })
        }
        return additions.length ? [...prev, ...additions] : prev
      })

      // Start polling fallback in case SSE connection is dead
      startPollForResponse(res.assistant_message_id)
    } catch (err) {
      if (!mountedRef.current) return
      logChatTransportError('Chat send', err, sessionChoiceRef.current, sendStartedAt)
      if (isTransientGatewayError(err)) {
        // The server may already have persisted the turn and pushed it over SSE
        // before the gateway timed out: never encourage a duplicate send until
        // history confirms whether the message landed.
        if (messagesRef.current.some(m => m.id > prevMaxId)) {
          const pending = pendingAssistantRef.current
          if (pending && !pollTimerRef.current) startPollForResponse(pending.id)
          return
        }
        pendingSendRef.current = { content: trimmed, prevMaxId, startedAt: sendStartedAt }
        recoveringSendRef.current = true
        setRecoveringSend(true)
        setTransientNotice(GATEWAY_SEND_RECOVERY_NOTICE)
        startPollForResponse(null, POLL_RECOVERY_DELAY_MS)
        return
      }
      setError(formatChatError(err, 'Failed to send message', GATEWAY_ACTION_ERROR))
      setInput(trimmed)
    } finally {
      sendInFlightRef.current = false
      if (mountedRef.current) setSending(false)
    }

    inputRef.current?.focus()
  }, [input, apiBasePath, sessionChoice, sessionStarted, clearing, sendBusy, startPollForResponse])

  const copyMessage = useCallback(async (content: string, msgId: number) => {
    try {
      await navigator.clipboard.writeText(content)
      setCopiedMsgId(msgId)
      setTimeout(() => setCopiedMsgId(null), 2000)
    } catch { /* clipboard not available */ }
  }, [])

  const copyAllMessages = useCallback(async () => {
    const text = messages
      .filter(m => m.content && m.status !== 'pending')
      .map(m => `${m.role === 'user' ? `**${m.username || 'User'}:**` : m.role === 'system' ? '**Server notice:**' : '**Assistant:**'}\n${m.content}`)
      .join('\n\n---\n\n')
    try {
      await navigator.clipboard.writeText(text)
      setCopiedAll(true)
      setTimeout(() => setCopiedAll(false), 2000)
    } catch { /* clipboard not available */ }
  }, [messages])

  const handleNewSession = useCallback(async () => {
    // A clear must not overtake the Start request it cannot see yet: before
    // the 202, the server has no preparation to answer 425, so a DELETE here
    // would be followed by the session this Start is about to create.
    if (startInFlightRef.current || clearingRef.current || clearing || sendInFlightRef.current) return
    clearingRef.current = true
    historyGenerationRef.current++
    setClearing(true)
    setError('')
    let resync = false
    try {
      // This preflight catches visible changes; only a server-side conditional DELETE can
      // protect an identical replacement or a session created after this GET.
      preflightRef.current = true
      const { history, generation } = await fetchMessages()
      preflightRef.current = false
      const active = history.active_session
      if (!mountedRef.current || historyGenerationRef.current !== generation) {
        resync = true
        console.info('[ChatUI] New Session cancelled: history changed during preflight')
        return
      }
      if (Boolean(active) !== sessionStarted || (active && (!sessionChoice || active.ai_provider !== sessionChoice.provider || active.ai_model !== sessionChoice.model || active.credential_source !== sessionChoice.credentialSource || sessionVersionRef.current !== history.active_session_version))) {
        setMessages(history.messages)
        applySession(active, history.active_session_version)
        console.info('[ChatUI] New Session cancelled: active session changed in another tab')
        return
      }
      await api.delete(apiBasePath, undefined, { headers: { 'If-Match': history.active_session_version } })
      historyGenerationRef.current++
      confirmedInitRef.current = false
      prepRunningRef.current = false
      pendingSendRef.current = null
      recoveringSendRef.current = false
      setRecoveringSend(false)
      setTransientNotice('')
      setStarting(false)
      cancelPoll()
      setSessionStarted(false)
      setSessionChoice(null)
      sessionVersionRef.current = null
      setMessages([])
      console.info('[ChatUI] Chat session cleared')
    } catch (err) {
      if (err instanceof ApiError && err.status === 409) {
        resync = true
        setError('Chat session changed in another tab. History refreshed; review it before clearing again.')
        console.info('[ChatUI] Chat clear conflict, refreshing history')
      } else {
        logChatTransportError('Chat clear', err, sessionChoiceRef.current, null)
        setError(formatChatError(err, 'Failed to clear chat', GATEWAY_ACTION_ERROR))
      }
    } finally {
      preflightRef.current = false
      clearingRef.current = false
      setClearing(false)
      if (resync && mountedRef.current) syncHistory()
    }
  }, [apiBasePath, cancelPoll, clearing, fetchMessages, sessionStarted, sessionChoice, applySession, syncHistory])

  const handleAbort = useCallback(async () => {
    try {
      await api.post(`${apiBasePath}/abort`, {})
      cancelPoll()
      setTransientNotice('')
      setMessages(prev => prev.map(m =>
        m.status === 'pending'
          ? { ...m, status: 'failed' as const, content: 'Aborted by user.' }
          : m
      ))
    } catch (err) {
      if (!mountedRef.current) return
      // A refused abort leaves the server row pending, so the turn really is still
      // running: say why, keep the bubble ticking, and let the completion sync
      // restore the composer. Flipping locally would re-enable it for the few
      // seconds until the next sync put the pending row back.
      logChatTransportError('Chat abort', err, sessionChoiceRef.current, pendingSinceMs())
      setError(formatChatError(err, 'Failed to stop the reply', GATEWAY_ACTION_ERROR))
    }
  }, [apiBasePath, cancelPoll, pendingSinceMs])

  const handleKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      handleSend()
    }
  }

  return (
    <TooltipProvider delayDuration={200}>
      <div className="flex flex-col h-[calc(100vh-6rem)]">
        {/* Header */}
        <div className="flex items-center justify-between border-b border-border-muted px-6 py-3 shrink-0">
          {header}
          <div className="flex items-center gap-3">
            {sessionStarted ? (
              <span className="text-xs text-text-secondary">{sessionChoice
                ? `${sessionChoice.provider} / ${sessionChoice.model}${sessionChoice.credentialSource === 'server' ? ' · Server' : ' · User'}`
                : 'Existing session · selection pinned by server'}</span>
            ) : <div className="flex flex-col items-end gap-1">
              <div className="flex items-center gap-3">
                <div className="w-[160px]"><AnalysisProviderSelect value={aiProvider} onChange={(v) => setAiPair(v, '')} forceServer={false} /></div>
                <div className="w-[240px]"><AnalysisModelSelect provider={aiProvider} value={aiModel} onChange={(model) => setAiPair(aiProvider, model, { commit: false })} onCommit={(model) => setAiPair(aiProvider, model)} forceServer={false} hideNotice /></div>
                <Button
                  size="sm"
                  onClick={handleStart}
                  disabled={!validPair || starting || clearing || loadingHistory || historyFailed}
                  aria-label="Start Chat"
                >
                  {starting ? <Loader2 className="h-4 w-4 animate-spin" /> : 'Start Chat'}
                </Button>
              </div>
              {/* Below the whole provider+model row, so it cannot distort the controls. */}
              <AnalysisModelNotice provider={aiProvider} forceServer={false} />
            </div>}
            <Tooltip>
              <TooltipTrigger asChild>
                <Button
                  variant="ghost"
                  size="sm"
                  className="h-7 px-2"
                  onClick={copyAllMessages}
                  disabled={messages.length === 0}
                  aria-label="Copy all messages"
                >
                  {copiedAll ? <Check className="h-3.5 w-3.5 text-signal-green" /> : <Copy className="h-3.5 w-3.5" />}
                </Button>
              </TooltipTrigger>
              <TooltipContent>{copiedAll ? 'Copied!' : 'Copy all messages'}</TooltipContent>
            </Tooltip>
            <Button
              variant="ghost"
              size="sm"
              className="h-7 px-3 text-xs"
              onClick={handleNewSession}
              disabled={sendBusy || clearing || loadingHistory}
            >
              New Session
            </Button>
          </div>
        </div>

        {!canUseServerProviders && !canUseFreeProviders && !sessionStarted && (
          <div className="px-6 pt-2">
            <CredentialAccessNotice />
          </div>
        )}
        {!sessionStarted && !loadingHistory && (
          <p className="px-6 pt-2 text-xs text-text-tertiary" role="status">Select an available AI provider and model, then Start Chat. History remains visible until you clear it.</p>
        )}
        {prepVisible && <div className="px-6 pt-2 text-xs" role="status">
          <p>{prepStatus}</p>
          {PREP_STEPS.map((step, i) => <StepIndicator
            key={step.phase}
            label={step.label}
            done={prepActiveIndex > i}
            active={prepActiveIndex === i} />)}
          <StepIndicator label="Load chat history" done={initStepIndex > 1} active={initStepIndex === 1} />
          <StepIndicator label="Ready" done={initStepIndex > 2} active={initStepIndex === 2} />
        </div>}

        {/* State transitions only — never the ticking label, which would re-announce every second. */}
        <span className="sr-only" role="status">
          {sending ? 'Sending message' : recoveringSend ? 'Reconnecting to check your message' : hasPending ? 'Waiting for the reply' : ''}
        </span>

        {/* Messages area */}
        <div className="flex-1 flex flex-col overflow-y-auto px-6 py-4 space-y-4">
          {loadingHistory && <p className="text-sm text-text-tertiary">Loading chat history...</p>}
          {!loadingHistory && messages.length === 0 && !sending && !recoveringSend && (
            <div className="flex flex-col items-center justify-center flex-1 text-text-tertiary">
              <Bot className="h-12 w-12 mb-3 opacity-30" />
              <p className="text-sm">{emptyMessage}</p>
              {emptySubtitle && <p className="text-xs mt-1">{emptySubtitle}</p>}
            </div>
          )}
          {messages.map(msg => msg.status === 'pending' ? (
            <ChatBusyRow
              key={msg.id}
              label="Thinking..."
              since={parseApiTimestamp(msg.created_at)}
              canStop={canStop}
              onStop={handleAbort}
              text={streamTexts[msg.id] ?? ''}
            />
          ) : msg.role === 'system' ? (
            // Server-written notice (an issue preview the user is asked to
            // confirm): rendered verbatim, not as the assistant's words.
            <div
              key={msg.id}
              className="rounded-lg border border-border-default bg-surface-elevated px-4 py-3 text-sm text-text-secondary"
            >
              <div className="flex items-center gap-2 text-[10px] uppercase tracking-widest text-text-tertiary">
                <Info className="h-3 w-3" />
                Server notice
              </div>
              <div className="mt-2 whitespace-pre-wrap break-words">
                <LinkedText text={msg.content} repoUrls={EMPTY_REPO_URLS} />
              </div>
              <div className="mt-2 text-[10px] text-text-tertiary">
                {new Date(msg.created_at).toLocaleString()}
              </div>
            </div>
          ) : (
            <div
              key={msg.id}
              className={`flex gap-3 ${msg.role === 'user' ? 'justify-end' : 'justify-start'}`}
            >
              {msg.role === 'assistant' && (
                <div className="shrink-0 mt-1">
                  <div className="h-7 w-7 rounded-full bg-accent-blue/15 flex items-center justify-center">
                    <Bot className="h-4 w-4 text-accent-blue" />
                  </div>
                </div>
              )}
              <div
                className={`max-w-[75%] rounded-lg px-4 py-3 text-sm ${
                  msg.role === 'user'
                    ? 'bg-accent-blue/15 text-text-primary'
                    : msg.status === 'failed'
                      ? 'bg-signal-red/10 text-signal-red border border-signal-red/20'
                      : 'bg-surface-elevated text-text-secondary'
                }`}
              >
                {msg.role === 'assistant' ? (
                  <ChatMarkdown content={msg.content} />
                ) : (
                  <div className="whitespace-pre-wrap break-words">
                    <LinkedText text={msg.content} repoUrls={EMPTY_REPO_URLS} />
                  </div>
                )}
                <div className="flex items-center justify-between mt-2">
                  <div className="flex items-center gap-2 text-[10px] text-text-tertiary">
                    {msg.role === 'user' && msg.username && <span>{msg.username}</span>}
                    {msg.role === 'assistant' && msg.ai_provider && (
                      <span>{msg.ai_provider}{msg.ai_model ? ` / ${msg.ai_model}` : ''}</span>
                    )}
                    {msg.status === 'failed' && <span className="text-signal-red">Failed</span>}
                    <span>{new Date(msg.created_at).toLocaleString()}</span>
                  </div>
                  <Tooltip>
                    <TooltipTrigger asChild>
                      <button
                        type="button"
                        className="text-text-tertiary hover:text-text-primary transition-colors"
                        onClick={() => copyMessage(msg.content, msg.id)}
                        aria-label="Copy message"
                      >
                        {copiedMsgId === msg.id ? <Check className="h-3 w-3 text-signal-green" /> : <Copy className="h-3 w-3" />}
                      </button>
                    </TooltipTrigger>
                    <TooltipContent>{copiedMsgId === msg.id ? 'Copied!' : 'Copy message'}</TooltipContent>
                  </Tooltip>
                </div>
              </div>
              {msg.role === 'user' && (
                <div className="shrink-0 mt-1">
                  <div className="h-7 w-7 rounded-full bg-surface-elevated flex items-center justify-center">
                    <User className="h-4 w-4 text-text-tertiary" />
                  </div>
                </div>
              )}
            </div>
          ))}
          {/* Below the thread, right above the sentinel. Rendering here does not by
              itself keep it visible: the auto-scroll effect keys on `sending` and `recoveringSend` too. */}
          {sending && !hasPending && <ChatBusyRow label="Sending..." canStop={false} onStop={handleAbort} />}
          {recoveringSend && !sending && !hasPending && (
            <ChatBusyRow
              label="Reconnecting..."
              since={pendingSendRef.current ? new Date(pendingSendRef.current.startedAt) : null}
              canStop={false}
              onStop={handleAbort}
            />
          )}
          <div ref={messagesEndRef} />
        </div>

        {/* Transient gateway notice — calm status while reconnecting on an active turn */}
        {transientNotice && (
          <div className="px-6 py-2 flex items-center gap-2 text-xs text-signal-amber" role="status">
            <Loader2 className="h-3.5 w-3.5 animate-spin shrink-0" />
            <p>{transientNotice}</p>
          </div>
        )}

        {/* Error */}
        {error && (
          <div className="px-6 py-2 flex items-center gap-2" role="alert">
            <p className="text-xs text-signal-red">{error}</p>
            {historyFailed && <Button size="sm" variant="ghost" onClick={() => { setError(''); setHistoryRetry(n => n + 1) }}>Retry history</Button>}
          </div>
        )}

        {/* Input area */}
        <div className="border-t border-border-muted px-6 py-3 shrink-0">
          <AttachmentPicker files={[]} onChange={() => {}} disabledReason="Browser chat cannot attach files. Use the report or feedback dialog, or the terminal with --attach." />
          <form onSubmit={handleSend} className="flex gap-2 mt-2">
            <Textarea
              ref={inputRef}
              value={input}
              onChange={e => {
                setInput(e.target.value)
                const el = e.target
                el.style.height = 'auto'
                el.style.height = `${Math.min(el.scrollHeight, 300)}px`
              }}
              onKeyDown={handleKeyDown}
              disabled={!sessionStarted || clearing || sendBusy}
              placeholder="Ask a question... (Enter to send, Shift+Enter for newline)"
              className="flex-1 min-h-[44px] max-h-[300px] resize-y overflow-y-auto"
              rows={1}
            />
            <Tooltip>
              <TooltipTrigger asChild>
                <span className="self-end inline-flex">
                  <Button
                    type="submit"
                    disabled={!canSend}
                    className="h-[44px] w-[44px] shrink-0"
                    aria-label={canSend ? 'Send message' : sendBlockedReason || 'Send message'}
                  >
                    <Send className="h-5 w-5" />
                  </Button>
                </span>
              </TooltipTrigger>
              {!canSend && sendBlockedReason && (
                <TooltipContent>{sendBlockedReason}</TooltipContent>
              )}
            </Tooltip>
          </form>
        </div>
      </div>
    </TooltipProvider>
  )
}
