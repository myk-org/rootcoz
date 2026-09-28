import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { it, expect } from 'vitest'
import { ModelCombobox } from './ModelCombobox'

it('labels a forced server model as locked when access is denied', async () => {
  render(<ModelCombobox value="" onChange={() => {}} options={[{
    id: 'sonnet', name: 'Sonnet', credential_sources: ['server'], serverLocked: true,
  }]} forceServer canUseServer={false} />)
  await userEvent.setup().click(screen.getByRole('combobox'))
  expect(screen.getByText('Server locked')).toBeInTheDocument()
  expect(screen.getByRole('option', { name: /sonnet.*Server locked/i })).toHaveAttribute('aria-disabled', 'true')
})
