import { useState, useEffect, useRef, useCallback, useMemo, type FormEvent, type KeyboardEvent } from 'react'
import { api, ApiError, extractApiDetail } from '@/lib/api'
import { useSSE } from '@/lib/useSSE'
import { Button } from '@/components/ui/button'
import { AnalysisModelNotice, AnalysisProviderSelect, AnalysisModelSelect } from '@/components/shared/AnalysisAiPicker'
import { isAnalysisAiAvailable } from '@/lib/analysisAi'
import { useProviderCatalog, useCursorAuthStatus } from '@/lib/useProviderOptions'
import { CredentialAccessNotice } from '@/components/shared/CredentialAccessNotice'
import { useAuth } from '@/lib/auth'
import { useAiSelection } from '@/lib/useAiSelection'
import { CursorAuthBanner } from '@/components/shared/CursorAuthBanner'
import { formatDuration, parseApiTimestamp, INVALID_DATE_FALLBACK } from '@/lib/utils'
import { Tooltip, TooltipTrigger, TooltipContent } from '@/components/ui/tooltip'
import { TooltipProvider } from '@/components/ui/tooltip'
import { Textarea } from '@/components/ui/textarea'
import { LinkedText } from '@/components/shared/LinkedText'
import { ChatMarkdown } from '@/components/shared/ChatMarkdown'
import type { RepoUrl } from '@/lib/autoLink'
import { Send, Loader2, Bot, User, Copy, Check, Info } from 'lucide-react'

const EMPTY_REPO_URLS: RepoUrl[] = []

/** Roles allowed to abort a turn — mirrors the server's `_require_reviewer`. */
const ABORT_ROLES = ['reviewer', 'operator', 'admin']

const INIT_STEPS = [
  'Initializing workspace and cloning repositories...',
  'Loading chat history...',
  'Ready',
] as const

/** Workspace-preparation phases reported by POST /init, in order. */
const PREP_STEPS = [
  { phase: 'preparing', label: 'Preparing workspace' },
  { phase: 'cloning', label: 'Cloning repositories' },
  { phase: 'indexing', label: 'Indexing repositories for search' },
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
function ChatBusyRow({ label, since, canStop, onStop }: { label: string; since?: Date | null; canStop: boolean; onStop: () => void }) {
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
  const [sessionStarted, setSessionStarted] = useState(false)
  const [sessionChoice, setSessionChoice] = useState<SessionChoice | null>(null)
  const [starting, setStarting] = useState(false)
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
  const cursorAuthStatus = useCursorAuthStatus()

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

  const hasPending = messages.some(m => m.status === 'pending')
  const sendBusy = sending || hasPending
  // Mirrors the server's `_require_reviewer` on abort_chat, which never checks that a
  // session exists. Gating on sessionStarted would hide Stop whenever a pending turn
  // outlives the session (credential or server grant revoked), wedging the composer.
  // A stale role still fails safely: the 403 surfaces and the bubble keeps ticking.
  const canStop = ABORT_ROLES.includes(role)

  const historyIsCurrent = useCallback((generation: number) =>
    mountedRef.current && !clearingRef.current && historyGenerationRef.current === generation, [])
  // Server-driven prep progress; initStepIndex > 0 means POST /init already returned.
  const prepActiveIndex = useMemo(() => {
    if (initStepIndex > 0) return PREP_STEPS.length
    const found = PREP_STEPS.findIndex(s => s.phase === prepPhase?.phase)
    return found === -1 ? 0 : found
  }, [initStepIndex, prepPhase])
  const prepStatus = prepPhase && initStepIndex === 0
    ? `${PREP_STEPS[prepActiveIndex]?.label ?? INIT_STEPS[0]}${prepPhase.detail ? `: ${prepPhase.detail}` : ''}...`
    : INIT_STEPS[initStepIndex]
  const validPair = isAnalysisAiAvailable(providers, providerStatus, aiProvider, aiModel, false, canUseServerProviders, canUseFreeProviders)
  const canSend = Boolean(sessionStarted && !clearing && input.trim() && !sendBusy)
  const sendBlockedReason = clearing ? 'Clearing chat session'
    : !sessionStarted
    ? 'Start Chat before sending'
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

  // Polling fallback: fetch messages until the specific assistant message resolves
  const startPollForResponse = useCallback((assistantMsgId: number) => {
    cancelPoll()
    const generation = pollGenerationRef.current
    const maxTime = Date.now() + 5 * 60 * 1000 // 5 min safety timeout
    console.debug('[ChatUI] Poll started — generation', generation, 'waiting for assistant msg', assistantMsgId)

    const poll = () => {
      if (pollGenerationRef.current !== generation) return
      if (Date.now() > maxTime) {
        console.warn('[ChatUI] Poll timed out after 5 minutes')
        pollTimerRef.current = null
        return
      }
      fetchMessages()
        .then(({ history, generation: historyGeneration }) => {
          if (pollGenerationRef.current !== generation || !historyIsCurrent(historyGeneration)) return
          const msgs = history.messages
          setMessages(msgs)
          setPrepPhase(history.preparing ?? null)
          // Check if the specific assistant message is no longer pending
          const hasResponse = msgs.some(m =>
            m.id === assistantMsgId && m.status !== 'pending'
          )
          if (hasResponse) {
            console.debug('[ChatUI] Poll completed — assistant response received')
            pollTimerRef.current = null
          } else {
            pollTimerRef.current = setTimeout(poll, 3000)
          }
        })
        .catch((err) => {
          if (pollGenerationRef.current !== generation || clearingRef.current || !mountedRef.current) return
          console.warn('[ChatUI] Poll fetch failed, retrying:', err instanceof Error ? err.message : 'unknown error')
          pollTimerRef.current = setTimeout(poll, 3000)
        })
    }

    // Start after short delay to give SSE a chance first
    pollTimerRef.current = setTimeout(poll, 2000)
  }, [fetchMessages, cancelPoll, historyIsCurrent])

  // History is readable without creating a workspace or AI session.
  useEffect(() => {
    let ignore = false
    const generation = historyGenerationRef.current
    setLoadingHistory(true)
    setHistoryFailed(false)
    fetchMessages()
      .then(({ history: res }) => {
        if (ignore || !historyIsCurrent(generation)) return
        const msgs = res.messages
        if (!ignore && historyIsCurrent(generation)) {
          setMessages(msgs)
          setPrepPhase(res.preparing ?? null)
          const active = res.active_session
          if (active || !confirmedInitRef.current) applySession(active, res.active_session_version)
          console.info('[ChatUI] Chat history loaded, active session:', !!active)
        }
      })
      .catch(err => { if (!ignore && historyIsCurrent(generation)) { setHistoryFailed(true); setError(err instanceof Error ? err.message : 'Failed to load chat history') } })
      // Not generation-gated: a send or a sync can invalidate this fetch, and the
      // spinner must still clear once the initial load has settled.
      .finally(() => { if (!ignore && mountedRef.current) setLoadingHistory(false) })
    return () => { ignore = true }
  }, [fetchMessages, historyRetry, historyIsCurrent, applySession])

  const handleStart = async () => {
    if (startInFlightRef.current || confirmedInitRef.current || starting || clearing || loadingHistory || historyFailed || sessionStarted || !validPair) return
    startInFlightRef.current = true
    setStarting(true)
    setInitStepIndex(0)
    setPrepPhase(null)
    setError('')
    console.info('[ChatUI] Starting chat session')
    try {
      const result = await api.post<{ ready: boolean; session_id?: string }>(`${apiBasePath}/init`, {
        ai_provider: aiProvider, ai_model: aiModel,
      })
      if (!mountedRef.current) return
      if (!result.ready || !result.session_id) throw new Error('Chat session unavailable. Clear chat and Start a new session.')
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
        setHistoryFailed(true)
        setError(err instanceof Error ? err.message : 'Failed to load chat history')
        console.warn('[ChatUI] Chat history failed after start')
      }
    } catch (err) {
      if (!mountedRef.current) return
      setError(extractApiDetail(err) ?? (err instanceof Error ? err.message : 'Failed to start chat'))
      console.warn('[ChatUI] Chat start failed')
    } finally {
      startInFlightRef.current = false
      if (mountedRef.current) setStarting(false)
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
        setMessages(history.messages)
        setPrepPhase(history.preparing ?? null)
        applySession(history.active_session, history.active_session_version)
        setLoadingHistory(false)
        setHistoryFailed(false)
        console.info('[ChatUI] Chat synchronized, active session:', !!history.active_session)
      })
      .catch((err) => {
        if (!historyIsCurrentRef.current(generation)) return
        console.warn('[ChatUI] Failed to sync chat:', err instanceof Error ? err.message : 'unknown error')
        setHistoryFailed(true)
        setError(err instanceof Error ? err.message : 'Failed to sync chat')
        setLoadingHistory(false)
      })
  }, [applySession])

  const chatEvents = useMemo(() => ({
    'chat-changed': () => {
      console.debug('[ChatUI] SSE chat-changed received, cancelling poll')
      cancelPollRef.current()
      syncHistory()
    },
  }), [syncHistory])

  const chatSseOnReconnect = useCallback(() => {
    console.debug('[ChatUI] SSE reconnected')
    cancelPollRef.current()
    syncHistory()
  }, [syncHistory])

  useSSE(sseTopic, chatEvents, { onReconnect: chatSseOnReconnect })

  // Auto-scroll — `sending` is in the deps because the transient Sending row is
  // rendered from that flag, not from `messages`, so appending it fires nothing.
  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages, sending])

  const handleSend = useCallback(async (e?: FormEvent) => {
    e?.preventDefault()
    const trimmed = input.trim()
    if (!trimmed || !sessionStarted || clearing || sendBusy || sendInFlightRef.current) return
    sendInFlightRef.current = true
    // A history fetch started before this send carries a snapshot without the
    // optimistic row; invalidate it so its late arrival cannot drop the row and
    // re-enable the composer mid-turn.
    historyGenerationRef.current++
    setSending(true)
    console.info('[ChatUI] Sending message')
    setError('')
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
      setError(err instanceof Error ? err.message : 'Failed to send message')
      setInput(trimmed)
      console.warn('[ChatUI] Send failed')
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
    if (clearingRef.current || clearing || sendInFlightRef.current) return
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
        setError(err instanceof Error ? err.message : 'Failed to clear chat')
        console.warn('[ChatUI] Chat clear failed')
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
      setError(extractApiDetail(err) ?? (err instanceof Error ? err.message : 'Failed to stop the reply'))
    }
  }, [apiBasePath])

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
                <Button size="sm" onClick={handleStart} disabled={!validPair || starting || clearing || loadingHistory || historyFailed}>
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
              disabled={sendBusy || starting || clearing || loadingHistory}
            >
              New Session
            </Button>
          </div>
        </div>

        {cursorAuthStatus && aiProvider === 'cursor' && (
          <div className="px-6 pt-3 shrink-0">
            <CursorAuthBanner status={cursorAuthStatus} />
          </div>
        )}

        {!canUseServerProviders && !canUseFreeProviders && !sessionStarted && (
          <div className="px-6 pt-2">
            <CredentialAccessNotice />
          </div>
        )}
        {!sessionStarted && !loadingHistory && (
          <p className="px-6 pt-2 text-xs text-text-tertiary" role="status">Select an available AI provider and model, then Start Chat. History remains visible until you clear it.</p>
        )}
        {starting && <div className="px-6 pt-2 text-xs" role="status">
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
          {sending ? 'Sending message' : hasPending ? 'Waiting for the reply' : ''}
        </span>

        {/* Messages area */}
        <div className="flex-1 flex flex-col overflow-y-auto px-6 py-4 space-y-4">
          {loadingHistory && <p className="text-sm text-text-tertiary">Loading chat history...</p>}
          {!loadingHistory && messages.length === 0 && !sending && (
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
              itself keep it visible: the auto-scroll effect keys on `sending` too. */}
          {sending && !hasPending && <ChatBusyRow label="Sending..." canStop={false} onStop={handleAbort} />}
          <div ref={messagesEndRef} />
        </div>

        {/* Error */}
        {error && (
          <div className="px-6 py-2 flex items-center gap-2" role="alert">
            <p className="text-xs text-signal-red">{error}</p>
            {historyFailed && <Button size="sm" variant="ghost" onClick={() => { setError(''); setHistoryRetry(n => n + 1) }}>Retry history</Button>}
          </div>
        )}

        {/* Input area */}
        <div className="border-t border-border-muted px-6 py-3 shrink-0">
          <form onSubmit={handleSend} className="flex gap-2">
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
