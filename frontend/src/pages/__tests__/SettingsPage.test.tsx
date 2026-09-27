import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { describe, expect, it, vi } from 'vitest'
import { SettingsPage } from '@/pages/SettingsPage'

let role = 'viewer'
vi.mock('@/lib/auth', () => ({ useAuth: () => ({ isAdmin: false, role, refreshAuth: vi.fn() }) }))
vi.mock('@/components/shared/ProfileForm', () => ({ ProfileForm: () => <div>Profile</div> }))
vi.mock('@/components/shared/AiCredentials', () => ({ AiCredentials: () => <div>AI provider keys</div> }))

describe('SettingsPage credential controls', () => {
  it('hides credential editing for viewers and shows it for reviewers', () => {
    role = 'viewer'
    const page = render(<MemoryRouter><SettingsPage /></MemoryRouter>)
    expect(screen.queryByText('AI provider keys')).not.toBeInTheDocument()
    role = 'reviewer'
    page.rerender(<MemoryRouter><SettingsPage /></MemoryRouter>)
    expect(screen.getByText('AI provider keys')).toBeInTheDocument()
  })
})
