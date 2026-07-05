import { useState, useEffect } from 'react'
import { useParams, Link } from 'react-router-dom'
import { api } from '@/lib/api'
import { Button } from '@/components/ui/button'
import { ChatUI } from '@/components/shared/ChatUI'
import { ArrowLeft } from 'lucide-react'

interface JobInfo {
  job_name: string
  build_number: number
  summary: string
  ai_provider: string
  ai_model: string
}

export function ChatPage() {
  const { jobId } = useParams<{ jobId: string }>()
  const [jobInfo, setJobInfo] = useState<JobInfo | null>(null)

  useEffect(() => {
    if (!jobId) return
    api.get<{ result: JobInfo }>(`/results/${jobId}`)
      .then(res => { if (res.result) setJobInfo(res.result) })
      .catch(() => {})
  }, [jobId])

  if (!jobId) return null

  const header = (
    <div className="flex items-center gap-3">
      <Button asChild variant="ghost" size="sm">
        <Link to={`/results/${jobId}`}>
          <ArrowLeft className="h-4 w-4 mr-1" />
          Results
        </Link>
      </Button>
      <div>
        <h1 className="text-sm font-display font-medium text-text-primary">
          Chat: {jobInfo?.job_name || jobId}
          {jobInfo?.build_number ? ` #${jobInfo.build_number}` : ''}
        </h1>
        <p className="text-xs text-text-tertiary truncate max-w-lg">{jobInfo?.summary}</p>
      </div>
    </div>
  )

  return (
    <ChatUI
      apiBasePath={`/api/chat/${jobId}`}
      sseTopic={`chat:${jobId}`}
      header={header}
      defaultProvider={jobInfo?.ai_provider || 'claude'}
      defaultModel={jobInfo?.ai_model || ''}
      emptyMessage="Ask a question about this analysis"
      emptySubtitle="The AI has access to all failure details, classifications, and repos"
    />
  )
}
