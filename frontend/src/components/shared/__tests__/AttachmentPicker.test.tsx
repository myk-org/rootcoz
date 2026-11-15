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
