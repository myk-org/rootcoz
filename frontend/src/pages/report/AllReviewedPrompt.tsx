import { useEffect, useMemo, useRef, useState } from 'react'
import { api } from '@/lib/api'
import { collectAllTestKeys } from '@/lib/failureKeys'
import { ConfirmDialog } from '@/components/shared/ConfirmDialog'
import { useReportState } from './ReportContext'

interface AllReviewedPromptProps {
  jobId: string
}

export function AllReviewedPrompt({ jobId }: AllReviewedPromptProps) {
  const { result, reviews, reportportalAvailable } = useReportState()
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

  useEffect(() => {
    function onReviewChanged(event: Event) {
      const { detail } = event as CustomEvent<{ jobId?: string }>
      if (detail?.jobId !== jobId) return
      // Wait for React to process the state update and re-render
      requestAnimationFrame(() => {
        const currentReviews = reviewsRef.current
        const currentKeys = allKeysRef.current
        if (!rpRef.current || currentKeys.length === 0) return
        const allNowReviewed = currentKeys.every((k) => currentReviews[k]?.reviewed)
        if (allNowReviewed) {
          setDialogOpen(true)
        }
      })
    }
    window.addEventListener('jji:review-changed', onReviewChanged)
    return () => window.removeEventListener('jji:review-changed', onReviewChanged)
  }, [jobId]) // re-subscribe when jobId changes

  // Reset on navigation
  useEffect(() => {
    setDialogOpen(false)
    setPushing(false)
  }, [jobId])

  const handleConfirm = async () => {
    setPushing(true)
    try {
      await api.post(`/results/${jobId}/push-reportportal`)
    } catch {
      // Best-effort — errors are not surfaced here
    } finally {
      setPushing(false)
      setDialogOpen(false)
    }
  }

  if (!reportportalAvailable) return null

  return (
    <ConfirmDialog
      open={dialogOpen}
      onOpenChange={setDialogOpen}
      title="All failures reviewed"
      description="All failures reviewed. Update Report Portal?"
      confirmLabel="Yes"
      cancelLabel="No"
      onConfirm={handleConfirm}
      loading={pushing}
    />
  )
}
