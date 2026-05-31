import { useState, useEffect, useRef, useCallback, type FormEvent, type KeyboardEvent } from 'react'
import { api } from '@/lib/api'
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
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [chatReady, setChatReady] = useState(false)
  const [initComplete, setInitComplete] = useState(false)
  const [initMessage, setInitMessage] = useState('')

  const [aiProvider, setAiProvider] = useState(defaultProvider)
  const [aiModel, setAiModel] = useState(defaultModel)
  const availableModels = useProviderModels(aiProvider)

  const [copiedMsgId, setCopiedMsgId] = useState<number | null>(null)

  const messagesEndRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLTextAreaElement>(null)

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

  // Init workspace in background, load history immediately
  useEffect(() => {
    let ignore = false
    setLoading(true)
    setChatReady(false)
    setInitMessage('Initializing...')

    // Init with timeout — don't block input indefinitely if init stalls
    const initTimeout = new Promise<void>((resolve) => setTimeout(resolve, 10000))
    Promise.race([
      api.post(`${apiBasePath}/init`, {}),
      initTimeout,
    ])
      .then(() => { if (!ignore) setInitComplete(true) })
      .catch(() => { if (!ignore) setInitComplete(true) })

    // Load history immediately (don't wait for init)
    fetchMessages()
      .then(msgs => {
        if (ignore) return
        setMessages(msgs)
        setChatReady(true)
        setInitMessage('')
      })
      .catch(err => {
        if (ignore) return
        setError(err instanceof Error ? err.message : 'Failed to load chat')
        setChatReady(true)
        setInitMessage('')
      })
      .finally(() => {
        if (!ignore) setLoading(false)
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

  // SSE: listen for chat message updates (AI responses)
  useEffect(() => {
    const evtSource = new EventSource(`${apiBasePath}/stream`)

    evtSource.addEventListener('chat-changed', () => {
      fetchMessages()
        .then(msgs => setMessages(msgs))
        .catch(() => {})  // Silently ignore fetch errors during SSE updates
    })

    evtSource.onerror = () => {
      // EventSource auto-reconnects
    }

    return () => evtSource.close()
  }, [apiBasePath, fetchMessages])

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
        assistant_message: { id: number; role: string; content: string; status: string }
      }>(apiBasePath, {
        message: trimmed,
        ai_provider: aiProvider || undefined,
        ai_model: aiModel || undefined,
      })

      // Add user message + pending assistant message
      setMessages(prev => [
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
        {
          id: res.assistant_message.id,
          job_id: '',
          role: 'assistant' as const,
          content: '',
          username: '',
          ai_provider: '',
          ai_model: '',
          status: 'pending',
          created_at: new Date().toISOString(),
        },
      ])
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to send message')
      setInput(trimmed)
    }

    inputRef.current?.focus()
  }, [input, apiBasePath, aiProvider, aiModel])

  const copyMessage = useCallback(async (content: string, msgId: number) => {
    try {
      await navigator.clipboard.writeText(content)
      setCopiedMsgId(msgId)
      setTimeout(() => setCopiedMsgId(null), 2000)
    } catch { /* clipboard not available */ }
  }, [])

  const handleNewSession = useCallback(async () => {
    setChatReady(false)
    setInitComplete(false)
    setInitMessage('Starting new session...')
    setMessages([])
    setError('')
    try {
      await api.delete(apiBasePath)
      // Init may take a while for repo cloning — don't block on it
      // Repos will be cloned on first message if init times out
      const initTimeout = new Promise<void>((resolve) => setTimeout(resolve, 10000))
      await Promise.race([
        api.post(`${apiBasePath}/init`, {}),
        initTimeout,
      ])
    } catch {
      // Init failure is non-fatal — chat still works, repos clone on first message
    } finally {
      setChatReady(true)
      setInitComplete(true)
      setInitMessage('')
    }
  }, [apiBasePath])

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

  if (loading || !chatReady) {
    return (
      <div className="flex flex-col items-center justify-center h-[60vh] gap-3">
        <Loader2 className="h-8 w-8 animate-spin text-accent-blue" />
        <p className="text-sm text-text-tertiary">{initMessage || 'Loading chat...'}</p>
      </div>
    )
  }

  return (
    <TooltipProvider delayDuration={200}>
      <div className="flex flex-col h-[calc(100vh-4rem)]">
        {/* Header */}
        <div className="flex items-center justify-between border-b border-border-muted px-6 py-3 shrink-0">
          {header}
          <div className="flex items-center gap-3">
            <ProviderSelect value={aiProvider} onChange={(v) => { setAiProvider(v); setAiModel(''); }} compact />
            <ModelCombobox
              value={aiModel}
              onChange={setAiModel}
              options={availableModels}
              placeholder="Model"
              className="w-[220px]"
            />
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
        <div className="flex-1 overflow-y-auto px-6 py-4 space-y-4">
          {messages.length === 0 && (
            <div className="flex flex-col items-center justify-center h-full text-text-tertiary">
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
              onChange={e => setInput(e.target.value)}
              onKeyDown={handleKeyDown}
              disabled={!initComplete}
              placeholder={initComplete ? "Ask a question... (Enter to send, Shift+Enter for newline)" : "Initializing workspace..."}
              className="flex-1 resize-none min-h-[44px] max-h-[120px]"
              rows={1}
            />
            <Button type="submit" disabled={!input.trim() || !chatReady || !initComplete} size="sm" className="self-end" aria-label="Send message">
              <Send className="h-4 w-4" />
            </Button>
          </form>
        </div>
      </div>
    </TooltipProvider>
  )
}
