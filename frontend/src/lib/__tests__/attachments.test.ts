import { afterEach, expect, it, vi } from 'vitest'
import {
  attachmentPickerPropsFromCapability,
  describeAttachmentDelivery,
  filesToAttachmentPayload,
} from '../attachments'
import type { Capabilities } from '@/types'

function makeCapabilities(
  overrides?: Partial<Capabilities['issue_attachments']>,
): Capabilities {
  return {
    github_issues_enabled: true,
    jira_issues_enabled: true,
    issue_attachments: {
      max_files: 3,
      max_file_bytes: 5242880,
      max_total_bytes: 10485760,
      types: ['text/plain', 'image/png', 'image/jpeg', 'image/gif', 'image/webp'],
      report_github: { available: true, reason: 'available' },
      report_jira: { available: true, reason: 'available' },
      feedback: { available: true, reason: 'available' },
      chat_ui: { available: false, reason: 'browser_chat_unsupported' },
      direct_chat: { available: true, reason: 'available' },
      ...overrides,
    },
  }
}

afterEach(() => vi.restoreAllMocks())
it('encodes full binary bytes and keeps input order without MIME metadata', async () => {
  expect(await filesToAttachmentPayload([new File([new Uint8Array([0, 255, 128])], 'a.png'), new File(['log'], 'b.log')])).toEqual([
    { filename: 'a.png', data_b64: 'AP+A' }, { filename: 'b.log', data_b64: 'bG9n' },
  ])
})
it('names the file when browser reading fails', async () => {
  vi.spyOn(FileReader.prototype, 'readAsDataURL').mockImplementation(function (this: FileReader) {
    this.dispatchEvent(new ProgressEvent('error'))
  })
  await expect(filesToAttachmentPayload([new File(['log'], 'broken.log')])).rejects.toThrow('Could not read broken.log')
})
it('returns no attachments for an empty selection', async () => {
  expect(await filesToAttachmentPayload([])).toEqual([])
})

it('maps pending, missing, and unavailable capability reasons and limit hints', () => {
  expect(attachmentPickerPropsFromCapability(null, false, 'feedback')).toEqual({
    disabledReason: 'Checking attachment capabilities...',
  })
  expect(attachmentPickerPropsFromCapability(null, true, 'feedback')).toEqual({
    disabledReason: 'Attachment capabilities are unavailable right now.',
  })
  expect(
    attachmentPickerPropsFromCapability(
      makeCapabilities({ report_github: { available: false, reason: 'public_base_url_required' } }),
      true,
      'report_github',
    ),
  ).toEqual({
    disabledReason: 'Attachments require PUBLIC_BASE_URL to be configured on this server.',
  })
  expect(
    attachmentPickerPropsFromCapability(
      makeCapabilities({ report_jira: { available: false, reason: 'tracker_disabled' } }),
      true,
      'report_jira',
    ),
  ).toEqual({
    disabledReason: 'Attachments are unavailable because this issue tracker is disabled on this server.',
  })
  expect(
    attachmentPickerPropsFromCapability(
      makeCapabilities({ feedback: { available: false, reason: 'browser_chat_unsupported' } }),
      true,
      'feedback',
    ),
  ).toEqual({
    disabledReason: 'Browser chat cannot attach files. Use the report or feedback dialog, or the terminal with --attach.',
  })
  expect(attachmentPickerPropsFromCapability(makeCapabilities(), true, 'report_github')).toEqual({
    hint: 'Up to 3 files (5 MiB each, 10 MiB total).',
  })
})

it('describes all wire-contract attachment delivery states and reasons', () => {
  expect(describeAttachmentDelivery({ filename: 'a.log', id: '1', delivery: 'hosted', reason: 'hosted' }))
    .toBe('Hosted link in issue body')
  expect(describeAttachmentDelivery({ filename: 'a.png', id: '2', delivery: 'native_and_hosted', reason: 'native_attached' }))
    .toBe('Attached in Jira + hosted link')
  expect(describeAttachmentDelivery({ filename: 'b.png', id: '3', delivery: 'fallback_only', reason: 'native_refused' }))
    .toBe('Hosted fallback link only (Jira refused native upload)')
  expect(describeAttachmentDelivery({ filename: 'c.png', id: '4', delivery: 'fallback_only', reason: 'native_unreachable' }))
    .toBe('Hosted fallback link only (Jira native upload unreachable)')
  expect(describeAttachmentDelivery({ filename: 'd.png', id: '5', delivery: 'fallback_only', reason: 'invalid_issue_key' }))
    .toBe('Hosted fallback link only (Jira key unavailable for native upload)')
  expect(describeAttachmentDelivery({ filename: 'e.png', id: '6', delivery: 'native_outcome_unknown', reason: 'native_outcome_unknown' }))
    .toBe('Jira native outcome unknown; hosted link in issue body')
})
