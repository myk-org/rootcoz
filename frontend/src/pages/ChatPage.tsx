import { useState, useEffect, useRef, useCallback, type FormEvent } from 'react'
import { useParams, Link } from 'react-router-dom'
import { api } from '@/lib/api'
import { Button } from '@/components/ui/button'
import {
  Select,
  SelectTrigger,
  SelectContent,
  SelectItem,
  SelectValue,
} from '@/components/ui/select'
import { ModelCombobox } from '@/components/shared/ModelCombobox'
import type { ModelOption } from '@/components/shared/ModelCombobox'
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
  const [sending, setSending] = useState(false)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [jobInfo, setJobInfo] = useState<JobInfo | null>(null)

  const [aiProvider, setAiProvider] = useState('')
  const [aiModel, setAiModel] = useState('')
  const [availableModels, setAvailableModels] = useState<ModelOption[]>([])
  const [aiConfigs, setAiConfigs] = useState<{ ai_provider: string; ai_model: string }[]>([])

  const [copiedMsgId, setCopiedMsgId] = useState<number | null>(null)

  const messagesEndRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLTextAreaElement>(null)
  const cancelledRef = useRef(false)

  // Load chat history + job info
  useEffect(() => {
    if (!jobId) return
    setLoading(true)

    Promise.all([
      api.get<{ messages: ChatMessage[]; total: number }>(`/api/chat/${jobId}`),
      api.get<{ result: { job_name: string; build_number: number; summary: string; ai_provider: string; ai_model: string } }>(`/results/${jobId}`),
      api.get<{ ai_provider: string; ai_model: string }[]>('/ai-configs'),
    ]).then(([chatRes, resultRes, configsRes]) => {
      setMessages(chatRes.messages)
      if (resultRes.result) {
        const r = resultRes.result
        setJobInfo({ job_name: r.job_name, build_number: r.build_number, summary: r.summary, ai_provider: r.ai_provider, ai_model: r.ai_model })
        setAiProvider(r.ai_provider || configsRes[0]?.ai_provider || '')
        setAiModel(r.ai_model || '')
      }
      setAiConfigs(configsRes)
    }).catch(err => {
      setError(err instanceof Error ? err.message : 'Failed to load chat')
    }).finally(() => setLoading(false))
  }, [jobId])

  // Fetch models when provider changes
  useEffect(() => {
    if (!aiProvider) { setAvailableModels([]); setAiModel(''); return }
    let ignore = false
    api.get<{ models: ModelOption[] }>(`/api/ai-models?provider=${aiProvider}`)
      .then(res => {
        if (ignore) return
        const models = res.models ?? []
        setAvailableModels(models)
        if (aiModel && !models.some(m => m.id === aiModel)) {
          setAiModel(models[0]?.id ?? '')
        }
      })
      .catch(() => { if (!ignore) { setAvailableModels([]); setAiModel('') } })
    return () => { ignore = true }
  }, [aiProvider])

  // Auto-scroll
  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages])

  const providers = [...new Set(aiConfigs.map(c => c.ai_provider))]

  const handleSend = useCallback(async (e?: FormEvent) => {
    e?.preventDefault()
    const trimmed = input.trim()
    if (!trimmed || sending || !jobId) return

    cancelledRef.current = false
    setSending(true)
    setError('')
    setInput('')

    // Optimistic user message
    const tempUserMsg: ChatMessage = {
      id: Date.now(),
      job_id: jobId,
      role: 'user',
      content: trimmed,
      username: '',
      ai_provider: '',
      ai_model: '',
      created_at: new Date().toISOString(),
    }
    setMessages(prev => [...prev, tempUserMsg])

    try {
      const res = await api.post<{
        user_message: { id: number; role: string; content: string; username: string }
        assistant_message: { id: number; role: string; content: string; ai_provider: string; ai_model: string }
      }>(`/api/chat/${jobId}`, {
        message: trimmed,
        ai_provider: aiProvider || undefined,
        ai_model: aiModel || undefined,
      })

      if (cancelledRef.current) return  // User clicked Stop

      // Replace optimistic message + add assistant response
      setMessages(prev => {
        const withoutTemp = prev.filter(m => m.id !== tempUserMsg.id)
        return [
          ...withoutTemp,
          { ...tempUserMsg, id: res.user_message.id, username: res.user_message.username },
          {
            id: res.assistant_message.id,
            job_id: jobId,
            role: 'assistant' as const,
            content: res.assistant_message.content,
            username: '',
            ai_provider: res.assistant_message.ai_provider,
            ai_model: res.assistant_message.ai_model,
            created_at: new Date().toISOString(),
          },
        ]
      })
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to send message')
      // Remove optimistic message on error
      setMessages(prev => prev.filter(m => m.id !== tempUserMsg.id))
      setInput(trimmed) // Restore input
    } finally {
      setSending(false)
      cancelledRef.current = false
      inputRef.current?.focus()
    }
  }, [input, sending, jobId, aiProvider, aiModel])

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

  if (loading) {
    return (
      <div className="flex items-center justify-center h-[60vh]">
        <Loader2 className="h-8 w-8 animate-spin text-text-tertiary" />
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
            {/* Provider selector */}
            <Select value={aiProvider} onValueChange={setAiProvider}>
              <SelectTrigger className="w-[120px] h-8 text-xs">
                <SelectValue placeholder="Provider" />
              </SelectTrigger>
              <SelectContent>
                {providers.map(p => (
                  <SelectItem key={p} value={p}>{p}</SelectItem>
                ))}
              </SelectContent>
            </Select>
            {/* Model selector */}
            <ModelCombobox
              value={aiModel}
              onChange={setAiModel}
              options={availableModels}
              placeholder="Model"
            />
            {/* Clear button */}
            <Tooltip>
              <TooltipTrigger asChild>
                <Button variant="ghost" size="sm" onClick={handleClear} disabled={messages.length === 0}>
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
                    : 'bg-surface-elevated text-text-secondary'
                }`}
              >
                <div className="whitespace-pre-wrap break-words">{msg.content}</div>
                <div className="flex items-center justify-between mt-2">
                  <div className="flex items-center gap-2 text-[10px] text-text-tertiary">
                    {msg.role === 'user' && msg.username && <span>{msg.username}</span>}
                    {msg.role === 'assistant' && msg.ai_provider && (
                      <span>{msg.ai_provider}{msg.ai_model ? ` / ${msg.ai_model}` : ''}</span>
                    )}
                    <span>{new Date(msg.created_at).toLocaleTimeString()}</span>
                  </div>
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
          {sending && (
            <div className="flex gap-3 justify-start">
              <div className="shrink-0 mt-1">
                <div className="h-7 w-7 rounded-full bg-accent-blue/15 flex items-center justify-center">
                  <Bot className="h-4 w-4 text-accent-blue" />
                </div>
              </div>
              <div className="flex items-center gap-2 bg-surface-elevated rounded-lg px-4 py-3">
                <Loader2 className="h-4 w-4 animate-spin text-text-tertiary" />
                <span className="text-xs text-text-tertiary">Thinking...</span>
                <Button variant="ghost" size="sm" className="h-6 px-2 text-xs text-signal-red" onClick={() => { cancelledRef.current = true; setSending(false) }}>
                  Stop
                </Button>
              </div>
            </div>
          )}
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
              disabled={sending}
            />
            <Button type="submit" disabled={!input.trim() || sending} size="sm" className="self-end">
              <Send className="h-4 w-4" />
            </Button>
          </form>
        </div>
      </div>
    </TooltipProvider>
  )
}
