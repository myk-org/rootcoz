import { useState, useEffect, useRef, useCallback, type FormEvent } from 'react'
import { useParams, Link } from 'react-router-dom'
import { api } from '@/lib/api'
import { Button } from '@/components/ui/button'
import { ProviderSelect } from '@/components/shared/ProviderSelect'
import { ModelCombobox } from '@/components/shared/ModelCombobox'
import { useProviderModels } from '@/hooks/useProviderModels'
import { Tooltip, TooltipTrigger, TooltipContent } from '@/components/ui/tooltip'
import { TooltipProvider } from '@/components/ui/tooltip'
import { Textarea } from '@/components/ui/textarea'
import { Send, Trash2, ArrowLeft, Loader2, Bot, User, Copy, Check } from 'lucide-react'

interface ChatMessage {
  id: number
  job_id: string
  role: 'user' | 'assistant'
  content: string
  username: string
  ai_provider: string
  ai_model: string
  status: string  // 'completed' | 'pending' | 'failed'
  created_at: string
}

interface JobInfo {
  job_name: string
  build_number: number
  summary: string
  ai_provider: string
  ai_model: string
}

export function ChatPage() {
  const { jobId } = useParams<{ jobId: string }>()
  const [messages, setMessages] = useState<ChatMessage[]>([])
  const [input, setInput] = useState('')
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [chatReady, setChatReady] = useState(false)
  const [initMessage, setInitMessage] = useState('')
  const [jobInfo, setJobInfo] = useState<JobInfo | null>(null)

  const [aiProvider, setAiProvider] = useState('')
  const [aiModel, setAiModel] = useState('')
  const availableModels = useProviderModels(aiProvider)

  const [copiedMsgId, setCopiedMsgId] = useState<number | null>(null)

  const messagesEndRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLTextAreaElement>(null)

  const hasPending = messages.some(m => m.status === 'pending')

  // Initialize chat workspace (clone repos)
  useEffect(() => {
    if (!jobId) return
    setChatReady(false)
    setInitMessage('Initializing workspace...')
    api.post<{ ready: boolean; repos_cloned: boolean; repo_names: string[] }>(`/api/chat/${jobId}/init`, {})
      .then(res => {
        if (res.repos_cloned && res.repo_names.length > 0) {
          setInitMessage(`Repos cloned: ${res.repo_names.join(', ')}`)
        }
        setChatReady(true)
      })
      .catch(() => {
        // Init failed but chat can still work without repos
        setChatReady(true)
        setInitMessage('')
      })
  }, [jobId])

  // Cleanup repos when leaving chat (keep sessions)
  useEffect(() => {
    return () => {
      if (jobId) {
        // Fire-and-forget cleanup — don't await
        api.post(`/api/chat/${jobId}/close`, {}).catch(() => {})
      }
    }
  }, [jobId])

  // Load chat history + job info
  useEffect(() => {
    if (!jobId) return
    setLoading(true)

    Promise.all([
      api.get<{ messages: ChatMessage[]; total: number }>(`/api/chat/${jobId}`),
      api.get<{ result: { job_name: string; build_number: number; summary: string; ai_provider: string; ai_model: string } }>(`/results/${jobId}`),
    ]).then(([chatRes, resultRes]) => {
      setMessages(chatRes.messages)
      if (resultRes.result) {
        const r = resultRes.result
        setJobInfo({ job_name: r.job_name, build_number: r.build_number, summary: r.summary, ai_provider: r.ai_provider, ai_model: r.ai_model })
        setAiProvider(r.ai_provider || 'claude')
        setAiModel(r.ai_model || '')
      }
    }).catch(err => {
      setError(err instanceof Error ? err.message : 'Failed to load chat')
    }).finally(() => setLoading(false))
  }, [jobId])

  // SSE: listen for chat message updates (AI responses)
  useEffect(() => {
    if (!jobId) return

    const evtSource = new EventSource(`/api/chat/${jobId}/stream`)

    evtSource.addEventListener('chat-changed', () => {
      // Re-fetch messages to get updated content/status
      api.get<{ messages: ChatMessage[]; total: number }>(`/api/chat/${jobId}`)
        .then(res => setMessages(res.messages))
        .catch(() => {})  // Silently ignore fetch errors during SSE updates
    })

    evtSource.onerror = () => {
      // EventSource auto-reconnects
    }

    return () => evtSource.close()
  }, [jobId])

  // Auto-scroll
  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages])

  const handleSend = useCallback(async (e?: FormEvent) => {
    e?.preventDefault()
    const trimmed = input.trim()
    if (!trimmed || !jobId) return

    setError('')
    setInput('')

    try {
      const res = await api.post<{
        user_message: { id: number; role: string; content: string; username: string; status: string }
        assistant_message: { id: number; role: string; content: string; status: string }
      }>(`/api/chat/${jobId}`, {
        message: trimmed,
        ai_provider: aiProvider || undefined,
        ai_model: aiModel || undefined,
      })

      // Add user message + pending assistant message
      setMessages(prev => [
        ...prev,
        {
          id: res.user_message.id,
          job_id: jobId,
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
          job_id: jobId,
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
  }, [input, jobId, aiProvider, aiModel])

  const copyMessage = useCallback(async (content: string, msgId: number) => {
    try {
      await navigator.clipboard.writeText(content)
      setCopiedMsgId(msgId)
      setTimeout(() => setCopiedMsgId(null), 2000)
    } catch { /* clipboard not available */ }
  }, [])

  const handleClear = useCallback(async () => {
    if (!jobId) return
    try {
      await api.delete(`/api/chat/${jobId}`)
      setMessages([])
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to clear chat')
    }
  }, [jobId])

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
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
          <div className="flex items-center gap-3">
            <Link to={`/results/${jobId}`}>
              <Button variant="ghost" size="sm">
                <ArrowLeft className="h-4 w-4 mr-1" />
                Results
              </Button>
            </Link>
            <div>
              <h1 className="text-sm font-display font-medium text-text-primary">
                Chat: {jobInfo?.job_name || jobId}
                {jobInfo?.build_number ? ` #${jobInfo.build_number}` : ''}
              </h1>
              <p className="text-xs text-text-tertiary truncate max-w-lg">{jobInfo?.summary}</p>
            </div>
          </div>
          <div className="flex items-center gap-3">
            <ProviderSelect value={aiProvider} onChange={setAiProvider} compact />
            <ModelCombobox
              value={aiModel}
              onChange={setAiModel}
              options={availableModels}
              placeholder="Model"
            />
            {/* Clear button */}
            <Tooltip>
              <TooltipTrigger asChild>
                <Button variant="ghost" size="sm" onClick={handleClear} disabled={messages.length === 0 || hasPending}>
                  <Trash2 className="h-4 w-4" />
                </Button>
              </TooltipTrigger>
              <TooltipContent>Clear chat history</TooltipContent>
            </Tooltip>
          </div>
        </div>

        {/* Messages area */}
        <div className="flex-1 overflow-y-auto px-6 py-4 space-y-4">
          {messages.length === 0 && (
            <div className="flex flex-col items-center justify-center h-full text-text-tertiary">
              <Bot className="h-12 w-12 mb-3 opacity-30" />
              <p className="text-sm">Ask a question about this analysis</p>
              <p className="text-xs mt-1">The AI has access to all failure details, classifications, and repos</p>
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
                  </div>
                ) : (
                  <div className="whitespace-pre-wrap break-words">{msg.content}</div>
                )}
                <div className="flex items-center justify-between mt-2">
                  <div className="flex items-center gap-2 text-[10px] text-text-tertiary">
                    {msg.role === 'user' && msg.username && <span>{msg.username}</span>}
                    {msg.role === 'assistant' && msg.ai_provider && (
                      <span>{msg.ai_provider}{msg.ai_model ? ` / ${msg.ai_model}` : ''}</span>
                    )}
                    {msg.status === 'failed' && <span className="text-signal-red">Failed</span>}
                    <span>{new Date(msg.created_at).toLocaleTimeString()}</span>
                  </div>
                  {msg.status !== 'pending' && (
                    <Tooltip>
                      <TooltipTrigger asChild>
                        <button
                          type="button"
                          className="text-text-tertiary hover:text-text-primary transition-colors"
                          onClick={() => copyMessage(msg.content, msg.id)}
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
              placeholder="Ask about this analysis... (Enter to send, Shift+Enter for newline)"
              className="flex-1 resize-none min-h-[44px] max-h-[120px]"
              rows={1}
            />
            <Button type="submit" disabled={!input.trim() || !chatReady} size="sm" className="self-end">
              <Send className="h-4 w-4" />
            </Button>
          </form>
        </div>
      </div>
    </TooltipProvider>
  )
}
