import { useState, useEffect, useRef, useCallback, useMemo, type FormEvent, type KeyboardEvent } from 'react'
import { api } from '@/lib/api'
import { useSharedSSE } from '@/lib/useSharedSSE'
import { Button } from '@/components/ui/button'
import { ProviderSelect } from '@/components/shared/ProviderSelect'
import { ModelCombobox } from '@/components/shared/ModelCombobox'
import { useProviderModels } from '@/hooks/useProviderModels'
import { Tooltip, TooltipTrigger, TooltipContent } from '@/components/ui/tooltip'
import { TooltipProvider } from '@/components/ui/tooltip'
import { Textarea } from '@/components/ui/textarea'
import { LinkedText } from '@/components/shared/LinkedText'
import { ChatMarkdown } from '@/components/shared/ChatMarkdown'
import type { RepoUrl } from '@/lib/autoLink'
import { Send, Loader2, Bot, User, Copy, Check } from 'lucide-react'

const EMPTY_REPO_URLS: RepoUrl[] = []

const INIT_STEPS = [
  'Initializing workspace and cloning repositories...',
  'Loading chat history...',
  'Ready',
] as const

export interface ChatMessage {
  id: number
  job_id: string
  role: 'user' | 'assistant'
  content: string
  username: string
  ai_provider: string
  ai_model: string
  status: string
  created_at: string
}

interface ChatUIProps {
  /** API base path — e.g. '/api/chat/job123' or '/api/admin/chat' */
  apiBasePath: string
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

export function ChatUI({
  apiBasePath,
  header,
  defaultProvider = 'claude',
  defaultModel = '',
  emptyMessage = 'Start a conversation',
  emptySubtitle = '',
}: ChatUIProps) {
  const [messages, setMessages] = useState<ChatMessage[]>([])
  const [input, setInput] = useState('')
  const [error, setError] = useState('')
  const [initComplete, setInitComplete] = useState(false)
  const [initStepIndex, setInitStepIndex] = useState(0)
  const [initError, setInitError] = useState('')

  const [aiProvider, setAiProvider] = useState(defaultProvider)
  const [aiModel, setAiModel] = useState(defaultModel)
  const availableModels = useProviderModels(aiProvider)

  const [copiedMsgId, setCopiedMsgId] = useState<number | null>(null)
  const [copiedAll, setCopiedAll] = useState(false)

  const messagesEndRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLTextAreaElement>(null)
  const pollTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const pollGenerationRef = useRef(0)

  const hasPending = messages.some(m => m.status === 'pending')

  // Sync provider/model from props when they change (e.g., after job info loads)
  useEffect(() => {
    if (defaultProvider) setAiProvider(defaultProvider)
  }, [defaultProvider])

  useEffect(() => {
    if (defaultModel) setAiModel(defaultModel)
  }, [defaultModel])


  // Fetch messages with pagination (last 200)
  const fetchMessages = useCallback(async (): Promise<ChatMessage[]> => {
    const res = await api.get<{ messages: ChatMessage[]; total: number }>(apiBasePath)
    if (res.total > 200) {
      const lastPage = await api.get<{ messages: ChatMessage[]; total: number }>(
        `${apiBasePath}?offset=${Math.max(res.total - 200, 0)}`
      )
      return lastPage.messages
    }
    return res.messages
  }, [apiBasePath])

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
        .then(msgs => {
          if (pollGenerationRef.current !== generation) return
          setMessages(msgs)
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
          if (pollGenerationRef.current !== generation) return
          console.warn('[ChatUI] Poll fetch failed, retrying:', err instanceof Error ? err.message : 'unknown error')
          pollTimerRef.current = setTimeout(poll, 3000)
        })
    }

    // Start after short delay to give SSE a chance first
    pollTimerRef.current = setTimeout(poll, 2000)
  }, [fetchMessages, cancelPoll])

  // Init workspace first, then load history
  useEffect(() => {
    let ignore = false
    setInitComplete(false)
    setInitError('')
    setInitStepIndex(0)

    // Step 1: Init (blocks until workspace + repos + session ready)
    api.post<{ ready: boolean; session_id?: string }>(
      `${apiBasePath}/init`, {}
    )
      .then(() => {
        if (ignore) return
        setInitStepIndex(1)
        // Step 2: Load history only after init completes
        return fetchMessages().then(msgs => {
          if (ignore) return
          setMessages(msgs)
          setInitStepIndex(2)
          setInitComplete(true)
        })
      })
      .catch(err => {
        if (ignore) return
        setInitError(err instanceof Error ? err.message : 'Failed to initialize chat')
        // Do NOT set initComplete — keep showing loading page with error + retry
      })

    return () => { ignore = true }
  }, [apiBasePath, fetchMessages])

  // Cleanup repos when leaving chat (keep sessions)
  useEffect(() => {
    return () => {
      // Fire-and-forget cleanup — don't await
      api.post(`${apiBasePath}/close`, {}).catch(() => {})
    }
  }, [apiBasePath])

  // Shared SSE: listen for chat message updates (AI responses)
  const fetchMessagesRef = useRef(fetchMessages)
  fetchMessagesRef.current = fetchMessages
  const cancelPollRef = useRef(cancelPoll)
  cancelPollRef.current = cancelPoll

  const chatEvents = useMemo(() => ({
    'chat-changed': () => {
      console.debug('[ChatUI] SSE chat-changed received, cancelling poll')
      cancelPollRef.current()
      fetchMessagesRef.current()
        .then(msgs => setMessages(msgs))
        .catch((err) => { console.warn('[ChatUI] Failed to sync messages:', err instanceof Error ? err.message : 'unknown error') })
    },
  }), [])

  const chatSseOnOpen = useCallback(() => {
    console.debug('[ChatUI] SSE connected')
    fetchMessagesRef.current()
      .then(msgs => setMessages(msgs))
      .catch((err) => { console.warn('[ChatUI] Failed to sync messages:', err instanceof Error ? err.message : 'unknown error') })
  }, [])

  useSharedSSE({
    url: initComplete ? `${apiBasePath}/stream` : null,
    events: chatEvents,
    onOpen: chatSseOnOpen,
  })

  // Auto-scroll
  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages])

  const handleSend = useCallback(async (e?: FormEvent) => {
    e?.preventDefault()
    const trimmed = input.trim()
    if (!trimmed) return

    setError('')
    setInput('')

    try {
      const res = await api.post<{
        user_message: { id: number; role: string; content: string; username: string; status: string }
        assistant_message_id: number
      }>(apiBasePath, {
        message: trimmed,
        ai_provider: aiProvider || undefined,
        ai_model: aiModel || undefined,
      })

      // Add user message only — assistant placeholder arrives via SSE when processing starts
      setMessages(prev => {
        // Dedupe: SSE may have already synced this message from the server
        if (prev.some(m => m.id === res.user_message.id)) return prev
        return [
          ...prev,
          {
            id: res.user_message.id,
            job_id: '',
            role: 'user' as const,
            content: trimmed,
            username: res.user_message.username,
            ai_provider: '',
            ai_model: '',
            status: 'completed',
            created_at: new Date().toISOString(),
          },
        ]
      })

      // Start polling fallback in case SSE connection is dead
      startPollForResponse(res.assistant_message_id)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to send message')
      setInput(trimmed)
    }

    inputRef.current?.focus()
  }, [input, apiBasePath, aiProvider, aiModel, startPollForResponse])

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
      .map(m => `${m.role === 'user' ? `**${m.username || 'User'}:**` : '**Assistant:**'}\n${m.content}`)
      .join('\n\n---\n\n')
    try {
      await navigator.clipboard.writeText(text)
      setCopiedAll(true)
      setTimeout(() => setCopiedAll(false), 2000)
    } catch { /* clipboard not available */ }
  }, [messages])

  const handleNewSession = useCallback(async () => {
    cancelPoll()
    setInitComplete(false)
    setInitStepIndex(0)
    setInitError('')
    setMessages([])
    setError('')
    try {
      await api.delete(apiBasePath)
      setInitStepIndex(0)
      await api.post(`${apiBasePath}/init`, {})
      setInitStepIndex(1)
      const msgs = await fetchMessages()
      setMessages(msgs)
      setInitStepIndex(2)
    } catch (err) {
      setInitError(err instanceof Error ? err.message : 'Failed to start new session')
      // Do NOT set initComplete — show error + retry on loading page
      return
    }
    setInitComplete(true)
  }, [apiBasePath, fetchMessages, cancelPoll])

  const handleAbort = useCallback(async () => {
    try {
      await api.post(`${apiBasePath}/abort`, {})
      // Optimistically mark pending messages as failed locally
      setMessages(prev => prev.map(m =>
        m.status === 'pending'
          ? { ...m, status: 'failed' as const, content: 'Aborted by user.' }
          : m
      ))
    } catch {
      // Abort is best-effort — SSE will update the message status
    }
  }, [apiBasePath])

  const handleKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      handleSend()
    }
  }

  if (!initComplete) {
    const stepLabels = ['Create workspace & AI session', 'Load chat history']
    return (
      <div className="flex flex-col items-center justify-center h-[60vh] gap-4">
        {!initError && <Loader2 className="h-8 w-8 animate-spin text-accent-blue" />}
        <div className="text-center space-y-3">
          <p className="text-sm font-medium text-text-primary">{INIT_STEPS[initStepIndex] ?? 'Initializing...'}</p>
          <div className="flex flex-col gap-1.5 text-xs">
            {stepLabels.map((label, i) => (
              <StepIndicator
                key={label}
                label={label}
                done={initStepIndex > i}
                active={initStepIndex === i}
              />
            ))}
          </div>
        </div>
        {initError && (
          <div className="text-center space-y-2">
            <p className="text-xs text-signal-red">{initError}</p>
            <Button variant="ghost" size="sm" onClick={() => {
              setInitError('')
              setInitStepIndex(0)
              api.post(`${apiBasePath}/init`, {})
                .then(() => {
                  setInitStepIndex(1)
                  return fetchMessages()
                })
                .then(msgs => {
                  if (msgs) setMessages(msgs)
                  setInitStepIndex(2)
                  setInitComplete(true)
                })
                .catch(err => setInitError(err instanceof Error ? err.message : 'Retry failed'))
            }}>
              Retry
            </Button>
          </div>
        )}
      </div>
    )
  }

  return (
    <TooltipProvider delayDuration={200}>
      <div className="flex flex-col h-[calc(100vh-6rem)]">
        {/* Header */}
        <div className="flex items-center justify-between border-b border-border-muted px-6 py-3 shrink-0">
          {header}
          <div className="flex items-center gap-3">
            <ProviderSelect value={aiProvider} onChange={(v) => { setAiProvider(v); setAiModel(''); }} compact />
            <ModelCombobox
              value={aiModel}
              onChange={setAiModel}
              options={availableModels}
              placeholder={availableModels[0]?.id || "Default model"}
              className="w-[400px]"
            />
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
              disabled={hasPending}
            >
              New Session
            </Button>
          </div>
        </div>

        {/* Messages area */}
        <div className="flex-1 flex flex-col overflow-y-auto px-6 py-4 space-y-4">
          {messages.length === 0 && (
            <div className="flex flex-col items-center justify-center flex-1 text-text-tertiary">
              <Bot className="h-12 w-12 mb-3 opacity-30" />
              <p className="text-sm">{emptyMessage}</p>
              {emptySubtitle && <p className="text-xs mt-1">{emptySubtitle}</p>}
            </div>
          )}
          {messages.map(msg => (
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
                {msg.status === 'pending' ? (
                  <div className="flex items-center gap-2 text-text-tertiary">
                    <Loader2 className="h-4 w-4 animate-spin" />
                    <span className="text-sm">Thinking...</span>
                    <Button
                      type="button"
                      variant="outline"
                      size="sm"
                      className="h-6 px-2 text-xs text-signal-red border-signal-red/30 hover:bg-signal-red/10 ml-2"
                      onClick={handleAbort}
                    >
                      Stop
                    </Button>
                  </div>
                ) : msg.role === 'assistant' ? (
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
                  {msg.status !== 'pending' && (
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
                  )}
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
          <div ref={messagesEndRef} />
        </div>

        {/* Error */}
        {error && (
          <div className="px-6 py-2">
            <p className="text-xs text-signal-red">{error}</p>
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
              placeholder="Ask a question... (Enter to send, Shift+Enter for newline)"
              className="flex-1 min-h-[44px] max-h-[300px] resize-y overflow-y-auto"
              rows={1}
            />
            <Button type="submit" disabled={!input.trim()} className="self-end h-[44px] w-[44px] shrink-0" aria-label="Send message">
              <Send className="h-5 w-5" />
            </Button>
          </form>
        </div>
      </div>
    </TooltipProvider>
  )
}
