import { useEffect, useMemo, useRef, useState } from 'react'
import { api } from '@/lib/api'
import { collectAllTestKeys } from '@/lib/failureKeys'
import { ConfirmDialog } from '@/components/shared/ConfirmDialog'
import { useReportState } from '@/pages/report/reportState'

interface AllReviewedPromptProps {
  jobId: string
}

export function AllReviewedPrompt({ jobId }: AllReviewedPromptProps) {
  const { result, reviews, reportportalAvailable, datadogAvailable } = useReportState()
  const [dialogOpen, setDialogOpen] = useState(false)
  const [pushing, setPushing] = useState(false)

  const allKeys = useMemo(
    () =>
      result
        ? collectAllTestKeys(
            result.failures ?? [],
            result.child_job_analyses ?? [],
          )
        : [],
    [result],
  )

  // Keep refs to latest values so the event handler always reads current state
  const reviewsRef = useRef(reviews)
  reviewsRef.current = reviews
  const allKeysRef = useRef(allKeys)
  allKeysRef.current = allKeys
  const rpRef = useRef(reportportalAvailable)
  rpRef.current = reportportalAvailable
  const datadogRef = useRef(datadogAvailable)
  datadogRef.current = datadogAvailable

  useEffect(() => {
    function onReviewChanged(event: Event) {
      const { detail } = event as CustomEvent<{ jobId?: string }>
      if (detail?.jobId !== jobId) return
      // Wait for React to process the state update and re-render
      requestAnimationFrame(() => {
        const currentReviews = reviewsRef.current
        const currentKeys = allKeysRef.current
        if ((!rpRef.current && !datadogRef.current) || currentKeys.length === 0) return
        const allNowReviewed = currentKeys.every((k) => currentReviews[k]?.reviewed)
        if (allNowReviewed) {
          setDialogOpen(true)
        }
      })
    }
    window.addEventListener('rootcoz:review-changed', onReviewChanged)
    return () => window.removeEventListener('rootcoz:review-changed', onReviewChanged)
  }, [jobId]) // re-subscribe when jobId changes

  // Reset on navigation
  useEffect(() => {
    setDialogOpen(false)
    setPushing(false)
  }, [jobId])

  const handleConfirm = async () => {
    setPushing(true)
    try {
      // Best-effort per exporter — errors are not surfaced here
      const pushes: Array<Promise<unknown>> = []
      if (rpRef.current) pushes.push(api.post(`/results/${jobId}/push-reportportal`))
      if (datadogRef.current) pushes.push(api.post(`/results/${jobId}/push/datadog`))
      await Promise.allSettled(pushes)
    } finally {
      setPushing(false)
      setDialogOpen(false)
    }
  }

  if (!reportportalAvailable && !datadogAvailable) return null

  const pushTargets =
    reportportalAvailable && datadogAvailable
      ? 'Report Portal and Datadog'
      : reportportalAvailable
        ? 'Report Portal'
        : 'Datadog'

  return (
    <ConfirmDialog
      open={dialogOpen}
      onOpenChange={setDialogOpen}
      title="All failures reviewed"
      description={`All failures reviewed. Update ${pushTargets}?`}
      confirmLabel="Yes"
      cancelLabel="No"
      onConfirm={handleConfirm}
      loading={pushing}
    />
  )
}
