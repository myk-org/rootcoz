import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, waitFor, within } from '@testing-library/react'
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

function renderFilter(overrides: Partial<Parameters<typeof TestNameFilter>[0]> = {}) {
  const props = {
    name: '',
    mode: 'contains' as const,
    onChange: vi.fn(),
    ...overrides,
  }
  render(<TestNameFilter {...props} />)
  return { props, box: screen.getByRole('combobox', { name: 'Select Test name' }) }
}

/** Type into the shared control's search field (its own input, not the trigger). */
async function typeQuery(text: string) {
  await userEvent.click(screen.getByRole('combobox', { name: 'Select Test name' }))
  await userEvent.type(screen.getByRole('combobox', { name: 'Search Test name' }), text)
}

describe('TestNameFilter', () => {
  beforeEach(() => {
    apiGet.mockResolvedValue({ test_names: [] })
  })
  afterEach(() => {
    apiGet.mockReset()
  })

  it('looks up the top failing names before anything is typed', async () => {
    renderFilter()

    await waitFor(() => expect(apiGet).toHaveBeenCalledTimes(1))
    // The empty query is the server's own "top failing names" listing.
    expect(apiGet.mock.calls[0][0]).toBe('/api/dashboard/test-names?q=&limit=20')
  })

  it('debounces typing into one lookup', async () => {
    const { props } = renderFilter()
    await waitFor(() => expect(apiGet).toHaveBeenCalledTimes(1))

    await typeQuery('checkout')

    // Nothing goes out per keystroke — the server is not asked six times.
    await waitFor(() => expect(apiGet).toHaveBeenCalledTimes(2))
    expect(apiGet.mock.calls[1][0]).toBe('/api/dashboard/test-names?q=checkout&limit=20')
    // Typing alone is not a filter: only a pick commits one.
    expect(props.onChange).not.toHaveBeenCalled()
  })

  it('lists suggestions with their failure counts', async () => {
    apiGet.mockResolvedValue({
      test_names: [
        { test_name: 'test_checkout', failure_count: 42 },
        { test_name: 'test_checkout_cancel', failure_count: 7 },
      ],
    })
    renderFilter()

    await typeQuery('checkout')

    const list = await screen.findByRole('listbox')
    await waitFor(() => expect(within(list).getByText('test_checkout')).toBeInTheDocument())
    // The count rides beside the name, and the value stays the name alone.
    expect(within(list).getByText('test_checkout')).toHaveTextContent('test_checkout')
    expect(list).toHaveTextContent('42')
    expect(list).toHaveTextContent('7')
  })

  it('commits a picked name exactly, wildcards and mode, all', async () => {
    // A `*` in a test name is a character: picking the suggestion must not turn
    // it into a pattern or drop any other character.
    const exact = 'test_checkout[cart*]_fail'
    apiGet.mockResolvedValue({ test_names: [{ test_name: exact, failure_count: 3 }] })
    const { props } = renderFilter()

    await typeQuery('checkout')
    const list = await screen.findByRole('listbox')
    await userEvent.click(await within(list).findByText(exact))

    // One call, carrying the name and the mode together.
    expect(props.onChange).toHaveBeenCalledWith(exact, 'contains')
    // The menu closes on a pick instead of covering the page.
    await waitFor(() => expect(screen.queryByRole('listbox')).toBeNull())
  })

  it('takes a typed fragment as the value', async () => {
    const { props } = renderFilter()

    await typeQuery('checkout')
    await userEvent.click(await screen.findByText('Add "checkout"'))

    expect(props.onChange).toHaveBeenCalledWith('checkout', 'contains')
  })

  it('shows the name a link carried, and clears it on demand', async () => {
    const props = { name: 'test_checkout', mode: 'contains' as const, onChange: vi.fn() }
    render(<TestNameFilter {...props} />)

    expect(screen.getByRole('combobox', { name: 'Select Test name' })).toHaveTextContent('test_checkout')
    // Reading the control is not an edit: nothing was written back to the page.
    expect(props.onChange).not.toHaveBeenCalled()

    await userEvent.click(screen.getByRole('button', { name: 'Clear test name' }))
    expect(props.onChange).toHaveBeenCalledWith('', 'contains')
  })

  it('follows the URL when it changes from outside: a preset fills the box, Clear all empties it', () => {
    const onChange = vi.fn()
    const { rerender } = render(<TestNameFilter name="" mode="contains" onChange={onChange} />)
    const box = screen.getByRole('combobox', { name: 'Select Test name' })

    // A preset (or back/forward) arrives with a name and a mode.
    rerender(<TestNameFilter name="test_payment" mode="starts_with" onChange={onChange} />)
    expect(box).toHaveTextContent('test_payment')
    expect(screen.getByRole('combobox', { name: 'Test name match mode' })).toHaveTextContent(
      'starts with',
    )

    // Clear all.
    rerender(<TestNameFilter name="" mode="contains" onChange={onChange} />)
    expect(box).not.toHaveTextContent('test_payment')
    expect(screen.queryByRole('button', { name: 'Clear test name' })).toBeNull()

    // Reading the box is not an edit: nothing was written back to the page.
    expect(onChange).not.toHaveBeenCalled()
  })

  it('commits the name again with a newly picked mode', async () => {
    const { props } = renderFilter({ name: 'test_checkout' })

    await userEvent.click(screen.getByRole('combobox', { name: 'Test name match mode' }))
    await userEvent.click(await screen.findByRole('option', { name: 'ends with' }))

    // One call, carrying the name and the mode together.
    expect(props.onChange).toHaveBeenLastCalledWith('test_checkout', 'ends_with')
  })

  it('holds a mode picked before there is a name until the name arrives', async () => {
    const { props } = renderFilter()

    await userEvent.click(screen.getByRole('combobox', { name: 'Test name match mode' }))
    await userEvent.click(await screen.findByRole('option', { name: 'exact' }))
    // A mode with nothing to match is not a filter: nothing is written for it.
    expect(props.onChange).not.toHaveBeenCalled()

    await typeQuery('checkout')
    await userEvent.click(await screen.findByText('Add "checkout"'))
    expect(props.onChange).toHaveBeenCalledWith('checkout', 'exact')
  })
})
