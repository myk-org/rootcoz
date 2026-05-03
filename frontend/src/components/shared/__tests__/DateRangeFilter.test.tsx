import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { DateRangeFilter } from '../DateRangeFilter'
import type { ComponentProps } from 'react'

const FROM_LABEL = 'Filter from date'
const TO_LABEL = 'Filter to date'
const CLEAR_LABEL = 'Clear date filter'

function renderDateRangeFilter(overrides: Partial<ComponentProps<typeof DateRangeFilter>> = {}) {
  const props = {
    from: '',
    to: '',
    onFromChange: vi.fn(),
    onToChange: vi.fn(),
    ...overrides,
  }
  const result = render(<DateRangeFilter {...props} />)
  return { ...result, props }
}

describe('DateRangeFilter', () => {
  it('renders both date inputs', () => {
    renderDateRangeFilter()
    expect(screen.getByLabelText(FROM_LABEL)).toBeDefined()
    expect(screen.getByLabelText(TO_LABEL)).toBeDefined()
  })

  it('calls onFromChange when from date changes', () => {
    const { props } = renderDateRangeFilter()
    fireEvent.change(screen.getByLabelText(FROM_LABEL), { target: { value: '2025-01-01' } })
    expect(props.onFromChange).toHaveBeenCalledWith('2025-01-01')
  })

  it('calls onToChange when to date changes', () => {
    const { props } = renderDateRangeFilter()
    fireEvent.change(screen.getByLabelText(TO_LABEL), { target: { value: '2025-12-31' } })
    expect(props.onToChange).toHaveBeenCalledWith('2025-12-31')
  })

  it('does not show clear button when both values are empty', () => {
    renderDateRangeFilter()
    expect(screen.queryByLabelText(CLEAR_LABEL)).toBeNull()
  })

  it('shows clear button when from is set', () => {
    renderDateRangeFilter({ from: '2025-01-01' })
    expect(screen.getByLabelText(CLEAR_LABEL)).toBeDefined()
  })

  it('shows clear button when to is set', () => {
    renderDateRangeFilter({ to: '2025-12-31' })
    expect(screen.getByLabelText(CLEAR_LABEL)).toBeDefined()
  })

  it('clears both values when clear button is clicked', () => {
    const { props } = renderDateRangeFilter({ from: '2025-01-01', to: '2025-12-31' })
    fireEvent.click(screen.getByLabelText(CLEAR_LABEL))
    expect(props.onFromChange).toHaveBeenCalledWith('')
    expect(props.onToChange).toHaveBeenCalledWith('')
  })

  it('sets max constraint on from input based on to value', () => {
    renderDateRangeFilter({ to: '2025-06-15' })
    expect(screen.getByLabelText(FROM_LABEL).getAttribute('max')).toBe('2025-06-15')
  })

  it('sets min constraint on to input based on from value', () => {
    renderDateRangeFilter({ from: '2025-01-01' })
    expect(screen.getByLabelText(TO_LABEL).getAttribute('min')).toBe('2025-01-01')
  })

  it('does not set max on from input when to is empty', () => {
    renderDateRangeFilter()
    expect(screen.getByLabelText(FROM_LABEL).getAttribute('max')).toBeNull()
  })

  it('does not set min on to input when from is empty', () => {
    renderDateRangeFilter()
    expect(screen.getByLabelText(TO_LABEL).getAttribute('min')).toBeNull()
  })

  it('calls onClear instead of onFromChange/onToChange when onClear is provided', () => {
    const onClear = vi.fn()
    const { props } = renderDateRangeFilter({ from: '2025-01-01', to: '2025-12-31', onClear })
    fireEvent.click(screen.getByLabelText(CLEAR_LABEL))
    expect(onClear).toHaveBeenCalledOnce()
    expect(props.onFromChange).not.toHaveBeenCalled()
    expect(props.onToChange).not.toHaveBeenCalled()
  })
})
