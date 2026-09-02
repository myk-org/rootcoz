import { useState, useCallback } from 'react'
import { api, extractApiDetail } from './api'
import { captureClientError } from './errorCapture'

export interface ExporterPushOptions {
  body?: unknown
  childJobName?: string
  childBuildNumber?: number
}

function withChildScope(path: string, options: ExporterPushOptions): string {
  const params = new URLSearchParams()
  if (options.childJobName) params.set('child_job_name', options.childJobName)
  if (options.childBuildNumber != null) {
    params.set('child_build_number', String(options.childBuildNumber))
  }
  const query = params.toString()
  return query ? `${path}?${query}` : path
}

export interface UseExporterPushResult<T> {
  confirmOpen: boolean
  setConfirmOpen: (v: boolean) => void
  pushing: boolean
  resultDialogOpen: boolean
  setResultDialogOpen: (v: boolean) => void
  pushResult: T | null
  pushFailed: boolean
  pushErrorMessage: string | null
  runPush: (path: string, options?: ExporterPushOptions) => Promise<void>
}

export function useExporterPush<T>(): UseExporterPushResult<T> {
  const [confirmOpen, setConfirmOpen] = useState(false)
  const [pushing, setPushing] = useState(false)
  const [resultDialogOpen, setResultDialogOpen] = useState(false)
  const [pushResult, setPushResult] = useState<T | null>(null)
  const [pushFailed, setPushFailed] = useState(false)
  const [pushErrorMessage, setPushErrorMessage] = useState<string | null>(null)

  const runPush = useCallback(async (
    path: string,
    options: ExporterPushOptions = {},
  ) => {
    setConfirmOpen(false)
    setPushing(true)
    setPushFailed(false)
    setPushResult(null)
    setPushErrorMessage(null)
    try {
      const scopedPath = withChildScope(path, options)
      const result = options.body === undefined
        ? await api.post<T>(scopedPath)
        : await api.post<T>(scopedPath, options.body)
      setPushResult(result)
      setResultDialogOpen(true)
    } catch (error) {
      captureClientError('Exporter push request failed')
      setPushFailed(true)
      setPushErrorMessage(
        extractApiDetail(error) ?? 'The exporter request failed. Check server logs for details.',
      )
      setResultDialogOpen(true)
    } finally {
      setPushing(false)
    }
  }, [])

  return {
    confirmOpen,
    setConfirmOpen,
    pushing,
    resultDialogOpen,
    setResultDialogOpen,
    pushResult,
    pushFailed,
    pushErrorMessage,
    runPush
  }
}
