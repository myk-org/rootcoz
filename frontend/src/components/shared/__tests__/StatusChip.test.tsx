import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { StatusChip } from '../StatusChip'

describe('StatusChip', () => {
  it('renders "Analysis Timed Out" for timeout status', () => {
    render(<StatusChip status="timeout" />)
    expect(screen.getByText('Analysis Timed Out')).toBeDefined()
  })

  it('renders "Completed" for completed status', () => {
    render(<StatusChip status="completed" />)
    expect(screen.getByText('Completed')).toBeDefined()
  })

  it('renders "Running" for running status', () => {
    render(<StatusChip status="running" />)
    expect(screen.getByText('Running')).toBeDefined()
  })

  it('renders "Failed" for failed status', () => {
    render(<StatusChip status="failed" />)
    expect(screen.getByText('Failed')).toBeDefined()
  })

  it('renders raw status text for unknown statuses', () => {
    render(<StatusChip status="custom-status" />)
    expect(screen.getByText('custom-status')).toBeDefined()
  })
})
