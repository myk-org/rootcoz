import { useEffect, useState } from 'react'
import { api } from '@/lib/api'
import type {
  Capabilities,
  IssueAttachmentFileSummary,
  IssueAttachmentUpload,
} from '@/types'

/** Read only at create time; never persist bytes or include them in AI previews. */
export async function filesToAttachmentPayload(files: File[]): Promise<IssueAttachmentUpload[]> {
  return Promise.all(files.map(file => new Promise<IssueAttachmentUpload>((resolve, reject) => {
    const reader = new FileReader()
    const fail = () => reject(new Error(`Could not read ${file.name}. Select the file again before creating the issue.`))
    reader.onerror = fail
    reader.onabort = fail
    reader.onload = () => {
      if (typeof reader.result !== 'string' || !reader.result.includes(',')) {
        fail()
        return
      }
      resolve({ filename: file.name, data_b64: reader.result.substring(reader.result.indexOf(',') + 1) })
    }
    try {
      reader.readAsDataURL(file)
    } catch {
      fail()
    }
  })))
}

export interface CapabilityReadState {
  loaded: boolean
  capabilities: Capabilities | null
}

/**
 * Read `/api/capabilities` when `enabled` flips true unless an already-loaded
 * capability snapshot (such as `ResultResponse.capabilities` on ReportPage) is
 * supplied. Resets on close so reopening reflects current server settings.
 */
export function useCapabilities(
  enabled: boolean,
  preloaded?: CapabilityReadState,
): CapabilityReadState {
  const [state, setState] = useState<CapabilityReadState>({
    loaded: false,
    capabilities: null,
  })
  const skipFetch = !!preloaded?.loaded
  useEffect(() => {
    if (!enabled || skipFetch) return
    let ignore = false
    api
      .get<Capabilities>('/api/capabilities')
      .then((caps) => {
        if (ignore) return
        setState({ loaded: true, capabilities: caps ?? null })
      })
      .catch(() => {
        if (ignore) return
        setState({ loaded: true, capabilities: null })
      })
    return () => {
      ignore = true
      setState((prev) => (prev.loaded ? { loaded: false, capabilities: null } : prev))
    }
  }, [enabled, skipFetch])
  return preloaded?.loaded ? preloaded : state
}

export function formatAttachmentBytes(bytes: number): string {
  const mib = 1024 * 1024
  return bytes >= mib && bytes % mib === 0
    ? `${bytes / mib} MiB`
    : `${bytes.toLocaleString()} bytes`
}

export type IssueAttachmentDialogSurface = 'report_github' | 'report_jira' | 'feedback'

export function attachmentPickerPropsFromCapability(
  capabilities: Capabilities | null | undefined,
  loaded: boolean,
  surface: IssueAttachmentDialogSurface,
): { disabledReason?: string; hint?: string } {
  if (!loaded) {
    return { disabledReason: 'Checking attachment capabilities...' }
  }
  const ia = capabilities?.issue_attachments
  const entry = ia?.[surface]
  if (!ia || !entry) {
    return { disabledReason: 'Attachment capabilities are unavailable right now.' }
  }
  if (!entry.available) {
    switch (entry.reason) {
      case 'public_base_url_required':
        return { disabledReason: 'Attachments require PUBLIC_BASE_URL to be configured on this server.' }
      case 'tracker_disabled':
        return { disabledReason: 'Attachments are unavailable because this issue tracker is disabled on this server.' }
      case 'browser_chat_unsupported':
        return { disabledReason: 'Browser chat cannot attach files. Use the report or feedback dialog, or the terminal with --attach.' }
      default:
        return { disabledReason: 'Attachments are unavailable on this server.' }
    }
  }
  return {
    hint: `Up to ${ia.max_files} files (${formatAttachmentBytes(ia.max_file_bytes)} each, ${formatAttachmentBytes(ia.max_total_bytes)} total).`,
  }
}

export function describeAttachmentDelivery(file: IssueAttachmentFileSummary): string {
  switch (file.delivery) {
    case 'hosted':
      return 'Hosted link in issue body'
    case 'native_and_hosted':
      return 'Attached in Jira + hosted link'
    case 'fallback_only':
      if (file.reason === 'native_refused') {
        return 'Hosted fallback link only (Jira refused native upload)'
      }
      if (file.reason === 'native_unreachable') {
        return 'Hosted fallback link only (Jira native upload unreachable)'
      }
      if (file.reason === 'invalid_issue_key') {
        return 'Hosted fallback link only (Jira key unavailable for native upload)'
      }
      return 'Hosted fallback link only'
    case 'native_outcome_unknown':
      return 'Jira native outcome unknown; hosted link in issue body'
  }
}
