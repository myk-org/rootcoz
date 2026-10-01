import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { it, expect } from 'vitest'
import { useState } from 'react'
import { ModelCombobox } from './ModelCombobox'

HTMLElement.prototype.scrollIntoView = () => {}

it('keeps a denied server model disabled without a locked label', async () => {
  render(<ModelCombobox value="" onChange={() => {}} options={[{
    id: 'sonnet', name: 'Sonnet', credential_sources: ['server'], serverLocked: true,
  }]} forceServer canUseServer={false} />)
  const user = userEvent.setup()
  await user.keyboard('{Tab}{ArrowDown}{ArrowDown}')
  expect(screen.getByRole('combobox')).toHaveFocus()
  expect(screen.getByRole('combobox')).toHaveAccessibleDescription('Organization access is unavailable. Ask an admin for access.')
  const option = screen.getByRole('option', { name: /sonnet.*Server/i })
  expect(option).toHaveAttribute('aria-disabled', 'true')
  expect(option).not.toHaveTextContent('locked')
})

it('mutes only Server on mixed models and explains access on hover and focus', async () => {
  const user = userEvent.setup()
  render(<ModelCombobox value="" onChange={() => {}} options={[{
    id: 'shared', name: 'Shared', credential_sources: ['user', 'server'],
  }]} canUseServer={false} />)
  const input = screen.getByRole('combobox')
  await user.keyboard('{Tab}{ArrowDown}{ArrowDown}')
  expect(input).toHaveFocus()
  expect(input).toHaveAccessibleDescription('Organization access is unavailable. Ask an admin for access.')
  const option = screen.getByRole('option', { name: /shared.*User \+ Server/ })
  expect(option).not.toHaveAttribute('aria-disabled')
  expect(within(option).getByText('Server')).toHaveClass('text-text-tertiary')
  await user.hover(option)
  expect(await screen.findByRole('tooltip')).toHaveTextContent('Organization access is unavailable. Ask an admin for access.')
  await user.unhover(option)
  await user.keyboard('{ArrowUp}{Tab}')
  expect(input).not.toHaveFocus()
})

// Regression: the model box used to render read-only whenever the provider's
// models were "verified" (strict={!unverified}), so users could only pick from
// the list and could not type a model ID. It must always be a real combobox.
it('always accepts typed model IDs, including ones not in the list', async () => {
  const user = userEvent.setup()
  function Harness() {
    const [value, setValue] = useState('')
    return <ModelCombobox value={value} onChange={setValue} options={[{ id: 'gemini-2.5-pro', name: 'Gemini 2.5 Pro' }]} />
  }
  render(<Harness />)
  const input = screen.getByRole('combobox')
  expect(input).not.toHaveAttribute('readonly')

  await user.type(input, 'gpt-5.6-luna')
  expect(input).toHaveValue('gpt-5.6-luna')
})

it('keeps the typed value verbatim instead of decorating it as unavailable', () => {
  render(<ModelCombobox value="not-in-catalog" onChange={() => {}} options={[{ id: 'gemini-2.5-pro', name: 'Gemini 2.5 Pro' }]} />)
  expect(screen.getByRole('combobox')).toHaveValue('not-in-catalog')
})

it('still filters the dropdown while typing', async () => {
  const user = userEvent.setup()
  render(<ModelCombobox value="2.5" onChange={() => {}} options={[
    { id: 'gemini-2.5-pro', name: 'Gemini 2.5 Pro' },
    { id: 'claude-opus-4-6-1m', name: 'Claude Opus' },
  ]} />)
  await user.click(screen.getByRole('combobox'))
  const options = screen.getAllByRole('option')
  expect(options).toHaveLength(1)
  expect(options[0]).toHaveTextContent('gemini-2.5-pro')
})

// Regression guard for the dead-end the fix above could otherwise introduce: a
// stale stored model must not empty the dropdown, or the user can neither type
// a new id nor pick a replacement.
it('still offers the full list when the stored value matches nothing', async () => {
  const user = userEvent.setup()
  render(<ModelCombobox value="missing-model" onChange={() => {}} options={[
    { id: 'gemini-2.5-pro', name: 'Gemini 2.5 Pro' },
    { id: 'claude-opus-4-6-1m', name: 'Claude Opus' },
  ]} />)
  await user.click(screen.getByRole('combobox'))
  expect(screen.getAllByRole('option')).toHaveLength(2)
})
