import { ChatUI } from '@/components/shared/ChatUI'
import { Bot } from 'lucide-react'

export function AdminChatPage() {
  const header = (
    <div className="flex items-center gap-3">
      <Bot className="h-5 w-5 text-accent-blue" />
      <div>
        <h1 className="text-sm font-display font-medium text-text-primary">Server Chat</h1>
        <p className="text-xs text-text-tertiary">Cross-job analytics, failure trends, user activity</p>
      </div>
    </div>
  )

  return (
    <ChatUI
      apiBasePath="/api/admin/chat"
      header={header}
      defaultProvider="claude"
      emptyMessage="Ask about server analytics"
      emptySubtitle="Query failure trends, user activity, test history across all jobs"
    />
  )
}
