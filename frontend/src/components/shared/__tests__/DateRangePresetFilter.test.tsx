import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { DateRangePresetFilter } from '../DateRangePresetFilter'
import type { ComponentProps } from 'react'

function renderFilter(overrides: Partial<ComponentProps<typeof DateRangePresetFilter>> = {}) {
  const props = {
    from: '',
    to: '',
    onChange: vi.fn(),
    ...overrides,
  }
  const result = render(<DateRangePresetFilter {...props} />)
  return { ...result, props }
}

describe('DateRangePresetFilter', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2025-06-15T12:00:00Z'))
  })
  afterEach(() => {
    vi.useRealTimers()
  })

  it('renders trigger button with "All time" by default', () => {
    renderFilter()
    expect(screen.getByRole('button', { name: 'Date range filter' })).toBeDefined()
    expect(screen.getByText('All time')).toBeDefined()
  })

  it('shows preset options when opened', () => {
    renderFilter()
    fireEvent.click(screen.getByRole('button', { name: 'Date range filter' }))
    expect(screen.getByText('Last 7 days')).toBeDefined()
    expect(screen.getByText('Last 14 days')).toBeDefined()
    expect(screen.getByText('Last 30 days')).toBeDefined()
    expect(screen.getByText('Last year')).toBeDefined()
    expect(screen.getByText('Custom range…')).toBeDefined()
  })

  it('applies "Last 7 days" preset with single onChange call', () => {
    const { props } = renderFilter()
    fireEvent.click(screen.getByRole('button', { name: 'Date range filter' }))
    fireEvent.click(screen.getByText('Last 7 days'))
    expect(props.onChange).toHaveBeenCalledOnce()
    expect(props.onChange).toHaveBeenCalledWith('2025-06-08', '2025-06-15')
  })

  it('applies "All time" preset (clears both dates in single call)', () => {
    const { props } = renderFilter({ from: '2025-01-01', to: '2025-06-15' })
    fireEvent.click(screen.getByRole('button', { name: 'Date range filter' }))
    fireEvent.click(screen.getByText('All time'))
    expect(props.onChange).toHaveBeenCalledOnce()
    expect(props.onChange).toHaveBeenCalledWith('', '')
  })

  it('shows "Last 7 days" label when matching preset is active', () => {
    renderFilter({ from: '2025-06-08', to: '2025-06-15' })
    expect(screen.getByText('Last 7 days')).toBeDefined()
  })

  it('shows custom date label for non-preset range', () => {
    renderFilter({ from: '2025-05-01', to: '2025-05-28' })
    expect(screen.getByText('May 1 – May 28')).toBeDefined()
  })

  it('reveals custom date inputs when "Custom range…" is clicked', () => {
    renderFilter()
    fireEvent.click(screen.getByRole('button', { name: 'Date range filter' }))
    fireEvent.click(screen.getByText('Custom range…'))
    expect(screen.getByLabelText('Custom from date')).toBeDefined()
    expect(screen.getByLabelText('Custom to date')).toBeDefined()
  })

  it('calls onChange with new from and existing to when custom from changes', () => {
    const { props } = renderFilter()
    fireEvent.click(screen.getByRole('button', { name: 'Date range filter' }))
    fireEvent.click(screen.getByText('Custom range…'))
    fireEvent.change(screen.getByLabelText('Custom from date'), { target: { value: '2025-03-01' } })
    expect(props.onChange).toHaveBeenCalledWith('2025-03-01', '')
  })

  it('calls onChange with existing from and new to when custom to changes', () => {
    const { props } = renderFilter({ from: '2025-01-01' })
    fireEvent.click(screen.getByRole('button', { name: 'Date range filter' }))
    fireEvent.click(screen.getByText('Custom range…'))
    fireEvent.change(screen.getByLabelText('Custom to date'), { target: { value: '2025-04-15' } })
    expect(props.onChange).toHaveBeenCalledWith('2025-01-01', '2025-04-15')
  })

  it('shows "From" label when only from is set', () => {
    renderFilter({ from: '2025-03-01' })
    expect(screen.getByText('From Mar 1')).toBeDefined()
  })

  it('shows "Until" label when only to is set', () => {
    renderFilter({ to: '2025-05-28' })
    expect(screen.getByText('Until May 28')).toBeDefined()
  })
})
