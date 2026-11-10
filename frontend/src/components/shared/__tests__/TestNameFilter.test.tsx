import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { TestNameFilter } from '@/components/shared/TestNameFilter'

const apiGet = vi.fn()
vi.mock('@/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api')>()
  return { ...actual, api: { get: (...args: unknown[]) => apiGet(...args) } }
})

// Radix Select opens through pointer capture, which jsdom lacks.
HTMLElement.prototype.hasPointerCapture = () => false
HTMLElement.prototype.setPointerCapture = () => {}
HTMLElement.prototype.releasePointerCapture = () => {}
HTMLElement.prototype.scrollIntoView = () => {}

const PLACEHOLDER = 'Filter by failing test name'

function renderFilter(overrides: Partial<Parameters<typeof TestNameFilter>[0]> = {}) {
  const props = {
    name: '',
    mode: 'contains' as const,
    onChange: vi.fn(),
    ...overrides,
  }
  render(<TestNameFilter {...props} />)
  return { props, input: screen.getByPlaceholderText(PLACEHOLDER) }
}

describe('TestNameFilter', () => {
  beforeEach(() => {
    apiGet.mockResolvedValue({ test_names: [] })
  })
  afterEach(() => {
    apiGet.mockReset()
  })

  it('debounces typing into one commit and one lookup', async () => {
    const { props, input } = renderFilter()

    await userEvent.type(input, 'checkout')

    // Nothing goes out per keystroke — the page is not refetched six times.
    expect(props.onChange).not.toHaveBeenCalled()
    expect(apiGet).not.toHaveBeenCalled()

    await waitFor(() => expect(props.onChange).toHaveBeenCalledWith('checkout', 'contains'))
    expect(props.onChange).toHaveBeenCalledTimes(1)
    expect(apiGet).toHaveBeenCalledTimes(1)
    expect(apiGet.mock.calls[0][0]).toBe('/api/dashboard/test-names?q=checkout&limit=20')
  })

  it('lists suggestions with their failure counts', async () => {
    apiGet.mockResolvedValue({
      test_names: [
        { test_name: 'test_checkout', failure_count: 42 },
        { test_name: 'test_checkout_cancel', failure_count: 7 },
      ],
    })
    const { input } = renderFilter()

    await userEvent.type(input, 'checkout')

    const option = await screen.findByRole('option', { name: /test_checkout\b/ })
    expect(option).toHaveTextContent('42')
    expect(screen.getByRole('option', { name: /test_checkout_cancel/ })).toHaveTextContent('7')
  })

  it('fills the box with the picked name exactly, wildcards and all', async () => {
    // A `*` in a test name is a character: picking the suggestion must not turn
    // it into a pattern or drop any other character.
    const exact = 'test_checkout[cart*]_fail'
    apiGet.mockResolvedValue({ test_names: [{ test_name: exact, failure_count: 3 }] })
    const { props, input } = renderFilter()

    await userEvent.type(input, 'checkout')
    await userEvent.click(await screen.findByRole('option', { name: exact }))

    expect(input).toHaveValue(exact)
    expect(props.onChange).toHaveBeenLastCalledWith(exact, 'contains')
    // The list closes on a pick instead of covering the page.
    expect(screen.queryByRole('option')).toBeNull()
  })

  it('takes a name that arrives from the URL without writing it back', async () => {
    const { props, input } = renderFilter({ name: 'test_checkout' })

    // A bookmark or a preset fills the box…
    expect(input).toHaveValue('test_checkout')
    await userEvent.click(input)
    await userEvent.tab()

    // …without being committed back to the page as a change.
    expect(props.onChange).not.toHaveBeenCalled()
  })

  it('follows the URL when it changes from outside: a preset fills the box, Clear all empties it', () => {
    const onChange = vi.fn()
    const { rerender } = render(
      <TestNameFilter name="" mode="contains" onChange={onChange} />,
    )
    const input = screen.getByPlaceholderText(PLACEHOLDER)

    // A preset (or back/forward) arrives with a name and a mode.
    rerender(<TestNameFilter name="test_payment" mode="starts_with" onChange={onChange} />)
    expect(input).toHaveValue('test_payment')
    expect(screen.getByRole('combobox', { name: 'Test name match mode' })).toHaveTextContent(
      'starts with',
    )

    // Clear all.
    rerender(<TestNameFilter name="" mode="contains" onChange={onChange} />)
    expect(input).toHaveValue('')

    // Reading the box is not an edit: nothing was written back to the page.
    expect(onChange).not.toHaveBeenCalled()
  })

  it('commits the text with a newly picked mode', async () => {
    const { props } = renderFilter({ name: 'test_checkout' })

    await userEvent.click(screen.getByRole('combobox', { name: 'Test name match mode' }))
    await userEvent.click(await screen.findByRole('option', { name: 'ends with' }))

    // One call, carrying the name and the mode together.
    expect(props.onChange).toHaveBeenLastCalledWith('test_checkout', 'ends_with')
  })
})
