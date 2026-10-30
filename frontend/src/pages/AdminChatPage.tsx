import { useState, useEffect } from 'react'
import { ChatUI } from '@/components/shared/ChatUI'
import { Bot, Loader2 } from 'lucide-react'
import { api } from '@/lib/api'
import { normalizeProvider } from '@/lib/aiProviders'
import type { DefaultServerSettings } from '@/types'

export function AdminChatPage() {
  const [defaults, setDefaults] = useState<{
    provider: string
    model: string
  } | null>(null)

  useEffect(() => {
    let ignore = false
    api
      .get<DefaultServerSettings>('/api/default-server-settings')
      .then((d) => {
        if (ignore) return
        setDefaults({
          provider: d.ai_provider ? normalizeProvider(d.ai_provider) : '',
          model: d.ai_model?.trim() || '',
        })
      })
      .catch(() => {
        if (!ignore) setDefaults({ provider: '', model: '' })
      })
    return () => {
      ignore = true
    }
  }, [])

  const header = (
    <div className="flex items-center gap-3">
      <Bot className="h-5 w-5 text-accent-blue" />
      <div>
        <h1 className="text-sm font-display font-medium text-text-primary">Server Chat</h1>
        <p className="text-xs text-text-tertiary">Cross-job analytics, failure trends, user activity</p>
      </div>
    </div>
  )

  if (defaults === null) {
    return (
      <div className="flex items-center justify-center h-[calc(100vh-6rem)] text-text-tertiary gap-2">
        <Loader2 className="h-4 w-4 animate-spin" />
        <span className="text-sm">Loading chat defaults…</span>
      </div>
    )
  }

  return (
    <ChatUI
      apiBasePath="/api/admin/chat"
      sseTopic="admin-chat"
      header={header}
      defaultProvider={defaults.provider}
      defaultModel={defaults.model}
      emptyMessage="Ask about server analytics"
      emptySubtitle="Query failure trends, user activity, test history across all jobs"
    />
  )
}
