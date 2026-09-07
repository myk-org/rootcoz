import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { JobAttribution } from '../JobAttribution'

describe('JobAttribution', () => {
  it('shows submitter and analyzer when they differ', () => {
    render(<JobAttribution submittedBy="alice" analyzedBy="bob" />)
    expect(screen.getByText('alice')).toBeDefined()
    expect(screen.getByText(/analyzed/)).toBeDefined()
    expect(screen.getByText(/bob/)).toBeDefined()
  })

  it('hides analyzer when it matches submitter', () => {
    render(<JobAttribution submittedBy="alice" analyzedBy="alice" />)
    expect(screen.getByText('alice')).toBeDefined()
    expect(screen.queryByText(/analyzed/)).toBeNull()
  })
})
