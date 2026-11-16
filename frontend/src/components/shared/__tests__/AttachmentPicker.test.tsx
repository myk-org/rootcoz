import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, expect, it, vi } from 'vitest'
import { AttachmentPicker } from '../AttachmentPicker'

afterEach(cleanup)

it('lists selected names and sizes, removes by index, and does not filter extensions', () => {
  const onChange = vi.fn()
  const files = [new File(['abc'], 'evidence.svg')]
  const { container } = render(<AttachmentPicker files={files} onChange={onChange} />)
  expect(screen.getByText('evidence.svg')).toBeInTheDocument()
  expect(screen.getByText('3 bytes')).toBeInTheDocument()
  const input = container.querySelector('input')!
  expect(input).not.toHaveAttribute('accept')
  fireEvent.change(input, { target: { files: [new File(['log'], 'run.log')] } })
  expect(onChange.mock.calls[0][0].map((f: File) => f.name)).toEqual(['evidence.svg', 'run.log'])
  fireEvent.click(screen.getByRole('button', { name: 'Remove evidence.svg' }))
  expect(onChange).toHaveBeenLastCalledWith([])
})

it('exposes a keyboard-focusable disabled reason with a house tooltip', async () => {
  const user = userEvent.setup()
  const { container } = render(<AttachmentPicker files={[]} onChange={vi.fn()} disabledReason="Capabilities unavailable" />)
  expect(screen.getByRole('button', { name: 'Attach files' })).toBeDisabled()
  expect(container.querySelector('input')).toBeDisabled()
  await user.tab()
  expect(await screen.findByRole('tooltip')).toHaveTextContent('Capabilities unavailable')
  expect(container.querySelector('[title]')).toBeNull()
})

it('rejects count, per-file size, and cumulative size before appending and clears on valid change, remove, or reset', () => {
  const onChange = vi.fn()
  const existing = [
    new File([new Uint8Array(4 * 1024 * 1024)], 'first.log'),
    new File([new Uint8Array(4 * 1024 * 1024)], 'second.log'),
  ]
  const { container, rerender } = render(<AttachmentPicker files={existing} onChange={onChange} />)
  const input = container.querySelector('input')!

  // 1) Count exceeded (2 existing + 2 selected = 4 > default 3)
  fireEvent.change(input, {
    target: { files: [new File(['a'], 'third.log'), new File(['b'], 'fourth.log')] },
  })
  expect(onChange).not.toHaveBeenCalled()
  expect(screen.getByRole('alert')).toHaveTextContent('Cannot attach more than 3 files (4 selected).')

  // 2) Per-file size exceeded (> default 5 MiB)
  fireEvent.change(input, {
    target: { files: [new File([new Uint8Array(5 * 1024 * 1024 + 1)], 'huge.png')] },
  })
  expect(onChange).not.toHaveBeenCalled()
  expect(screen.getByRole('alert')).toHaveTextContent('huge.png (5,242,881 bytes) exceeds the 5 MiB per-file limit.')

  // 3) Cumulative size exceeded (8 MiB existing + 3 MiB selected = 11 MiB > default 10 MiB)
  fireEvent.change(input, {
    target: { files: [new File([new Uint8Array(3 * 1024 * 1024)], 'third.log')] },
  })
  expect(onChange).not.toHaveBeenCalled()
  expect(screen.getByRole('alert')).toHaveTextContent('Selected files total 11 MiB, exceeding the 10 MiB total limit.')

  // 4) Valid selection at exact boundary (8 MiB + 2 MiB = 10 MiB) clears error and appends
  const exactFit = new File([new Uint8Array(2 * 1024 * 1024)], 'fits.svg')
  fireEvent.change(input, { target: { files: [exactFit] } })
  expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  expect(onChange).toHaveBeenCalledTimes(1)
  expect(onChange.mock.calls[0][0].map((f: File) => f.name)).toEqual(['first.log', 'second.log', 'fits.svg'])

  // 5) Trigger another rejection, then verify Remove clears it
  fireEvent.change(input, {
    target: { files: [new File([new Uint8Array(3 * 1024 * 1024)], 'too-much.log')] },
  })
  expect(screen.getByRole('alert')).toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: 'Remove first.log' }))
  expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  expect(onChange).toHaveBeenLastCalledWith([existing[1]])

  // 6) Trigger another rejection with custom limits, then verify parent reset clears it
  rerender(
    <AttachmentPicker
      files={existing}
      onChange={onChange}
      maxFiles={2}
      maxFileBytes={4 * 1024 * 1024}
      maxTotalBytes={8 * 1024 * 1024}
    />,
  )
  fireEvent.change(input, { target: { files: [new File(['x'], 'extra.log')] } })
  expect(screen.getByRole('alert')).toHaveTextContent('Cannot attach more than 2 files (3 selected).')
  rerender(
    <AttachmentPicker
      files={[]}
      onChange={onChange}
      maxFiles={2}
      maxFileBytes={4 * 1024 * 1024}
      maxTotalBytes={8 * 1024 * 1024}
    />,
  )
  expect(screen.queryByRole('alert')).not.toBeInTheDocument()
})
