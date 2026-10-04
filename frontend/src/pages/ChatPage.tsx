import { useState, useEffect } from 'react'
import { useParams, Link } from 'react-router-dom'
import { api } from '@/lib/api'
import { Button } from '@/components/ui/button'
import { ChatUI } from '@/components/shared/ChatUI'
import { ArrowLeft, Loader2 } from 'lucide-react'
import { normalizeProvider } from '@/lib/aiProviders'

import { resolveBuildDisplayId } from '@/lib/utils'

interface JobInfo {
  job_name: string
  build_number?: number | string
  build_id?: string
  summary: string
  ai_provider: string
  ai_model: string
  request_params?: {
    ai_provider?: string
    ai_model?: string
    [key: string]: unknown
  }
}

/** Provider/model used for the job analysis — top-level result, then request_params. */
function analysisAiDefaults(job: JobInfo | null): { provider: string; model: string } {
  if (!job) return { provider: '', model: '' }
  const rp = job.request_params
  const rpProvider = typeof rp?.ai_provider === 'string' ? rp.ai_provider : ''
  const rpModel = typeof rp?.ai_model === 'string' ? rp.ai_model : ''
  const provider = normalizeProvider(job.ai_provider || rpProvider || '')
  const model = (job.ai_model || rpModel || '').trim()
  return { provider, model }
}

export function ChatPage() {
  const { jobId } = useParams<{ jobId: string }>()
  // Job info is loaded asynchronously; tag it with the job it belongs to so a
  // result for a previously visited job is never shown for the current one.
  const [jobState, setJobState] = useState<{ jobId: string; info: JobInfo | null } | null>(null)

  useEffect(() => {
    if (!jobId) return
    let ignore = false
    api
      .get<{ result: JobInfo }>(`/results/${jobId}`)
      .then((res) => {
        if (ignore) return
        setJobState({ jobId, info: res.result ?? null })
      })
      .catch(() => {
        if (ignore) return
        setJobState({ jobId, info: null })
      })
    return () => {
      ignore = true
    }
  }, [jobId])

  if (!jobId) return null

  const currentJobState = jobState?.jobId === jobId ? jobState : null
  const jobInfo = currentJobState?.info ?? null
  const jobLoaded = currentJobState !== null

  const { provider, model } = analysisAiDefaults(jobInfo)
  const buildDisplayId = resolveBuildDisplayId(jobInfo ?? undefined)

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
          {buildDisplayId ? ` #${buildDisplayId}` : ''}
        </h1>
        <p className="text-xs text-text-tertiary truncate max-w-lg">{jobInfo?.summary}</p>
      </div>
    </div>
  )

  if (!jobLoaded) {
    return (
      <div className="flex flex-col h-[calc(100vh-6rem)]">
        <div className="flex items-center justify-between border-b border-border-muted px-6 py-3 shrink-0">
          {header}
        </div>
        <div className="flex items-center justify-center flex-1 text-text-tertiary gap-2">
          <Loader2 className="h-4 w-4 animate-spin" />
          <span className="text-sm">Loading job analysis defaults…</span>
        </div>
      </div>
    )
  }

  return (
    <ChatUI
      apiBasePath={`/api/chat/${jobId}`}
      sseTopic={`chat:${jobId}`}
      header={header}
      defaultProvider={provider}
      defaultModel={model}
      emptyMessage="Ask a question about this analysis"
      emptySubtitle="The AI has access to all failure details, classifications, and repos"
    />
  )
}
