import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { it, expect } from 'vitest'
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
