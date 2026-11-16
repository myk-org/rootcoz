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
      max_run_bytes: 209715200,
      max_owner_bytes: 524288000,
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

it('validates count, per-file size, and cumulative size before starting any FileReader', async () => {
  const readSpy = vi.spyOn(FileReader.prototype, 'readAsDataURL')

  await expect(
    filesToAttachmentPayload([
      new File(['1'], '1.log'),
      new File(['2'], '2.log'),
      new File(['3'], '3.log'),
      new File(['4'], '4.log'),
    ]),
  ).rejects.toThrow('Cannot attach more than 3 files (4 selected).')
  expect(readSpy).not.toHaveBeenCalled()

  await expect(
    filesToAttachmentPayload([new File([new Uint8Array(5 * 1024 * 1024 + 1)], 'oversize.png')]),
  ).rejects.toThrow('oversize.png (5,242,881 bytes) exceeds the 5 MiB per-file limit.')
  expect(readSpy).not.toHaveBeenCalled()

  await expect(
    filesToAttachmentPayload([
      new File([new Uint8Array(4 * 1024 * 1024)], 'part1.log'),
      new File([new Uint8Array(4 * 1024 * 1024)], 'part2.log'),
      new File([new Uint8Array(3 * 1024 * 1024)], 'part3.log'),
    ]),
  ).rejects.toThrow('Selected files total 11 MiB, exceeding the 10 MiB total limit.')
  expect(readSpy).not.toHaveBeenCalled()

  await expect(
    filesToAttachmentPayload([new File(['abcde'], 'custom.log')], {
      maxFiles: 1,
      maxFileBytes: 4,
      maxTotalBytes: 8,
    }),
  ).rejects.toThrow('custom.log (5 bytes) exceeds the 4 bytes per-file limit.')
  expect(readSpy).not.toHaveBeenCalled()
})

it('reads files sequentially rather than concurrently and stops on first read error', async () => {
  let activeReaders = 0
  let maxConcurrentReaders = 0
  const startedNames: string[] = []

  vi.spyOn(FileReader.prototype, 'readAsDataURL').mockImplementation(function (this: FileReader, blob: Blob) {
    const file = blob as File
    startedNames.push(file.name)
    activeReaders += 1
    maxConcurrentReaders = Math.max(maxConcurrentReaders, activeReaders)
    setTimeout(() => {
      activeReaders -= 1
      Object.defineProperty(this, 'result', {
        configurable: true,
        value: `data:text/plain;base64,${btoa(file.name)}`,
      })
      this.dispatchEvent(new ProgressEvent('load'))
    }, 5)
  })

  const payload = await filesToAttachmentPayload([
    new File(['a'], 'first.log'),
    new File(['b'], 'second.log'),
    new File(['c'], 'third.log'),
  ])
  expect(maxConcurrentReaders).toBe(1)
  expect(startedNames).toEqual(['first.log', 'second.log', 'third.log'])
  expect(payload).toEqual([
    { filename: 'first.log', data_b64: btoa('first.log') },
    { filename: 'second.log', data_b64: btoa('second.log') },
    { filename: 'third.log', data_b64: btoa('third.log') },
  ])

  startedNames.length = 0
  vi.spyOn(FileReader.prototype, 'readAsDataURL').mockImplementation(function (this: FileReader, blob: Blob) {
    startedNames.push((blob as File).name)
    this.dispatchEvent(new ProgressEvent('error'))
  })
  await expect(
    filesToAttachmentPayload([new File(['a'], 'bad-first.log'), new File(['b'], 'never-read.log')]),
  ).rejects.toThrow('Could not read bad-first.log')
  expect(startedNames).toEqual(['bad-first.log'])
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
    maxFiles: 3,
    maxFileBytes: 5242880,
    maxTotalBytes: 10485760,
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
