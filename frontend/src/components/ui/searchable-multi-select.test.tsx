import { useState } from 'react'
import { fireEvent, render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import { SearchableMultiSelect } from './searchable-multi-select'
import type { SearchableMultiSelectOption } from './searchable-multi-select'

const OPTIONS: SearchableMultiSelectOption[] = [
  { value: 'gemini' },
  { value: 'admin' },
  { value: 'badminton' },
  { value: 'api-gateway' },
  { value: 'slack', disabled: true, hint: 'not configured' },
]

function Harness({
  options = OPTIONS,
  allowCustom = false,
  loading = false,
  initial = [],
}: {
  options?: SearchableMultiSelectOption[]
  allowCustom?: boolean
  loading?: boolean
  initial?: string[]
}) {
  const [values, setValues] = useState(initial)
  return (
    <SearchableMultiSelect
      label="ALLOWED_USERS"
      values={values}
      options={options}
      allowCustom={allowCustom}
      loading={loading}
      onChange={setValues}
    />
  )
}

const open = () => fireEvent.click(screen.getByRole('combobox', { name: 'Add ALLOWED_USERS' }))
const search = () => screen.getByRole('combobox', { name: 'Search ALLOWED_USERS' })
const options = () => screen.getAllByRole('option').map(option => option.textContent)

const STATIC: SearchableMultiSelectOption[] = [
  { value: 'viewer' },
  { value: 'reviewer' },
  { value: 'operator' },
]

// The same control serves every enumerated setting (E2E-F23): one choice,
// searchable even though the list has three entries.
function SingleHarness({
  options = STATIC,
  initial = [],
  onSave,
}: {
  options?: SearchableMultiSelectOption[]
  initial?: string[]
  onSave?: (value: string) => void
}) {
  const [values, setValues] = useState(initial)
  return (
    <SearchableMultiSelect
      label="DEFAULT_USER_ROLE"
      multiple={false}
      values={values}
      options={options}
      onChange={next => {
        setValues(next)
        onSave?.(next[0])
      }}
    />
  )
}

describe('SearchableMultiSelect single mode', () => {
  const openSingle = () =>
    fireEvent.click(screen.getByRole('combobox', { name: 'Select DEFAULT_USER_ROLE' }))
  const searchSingle = () =>
    screen.getByRole('combobox', { name: 'Search DEFAULT_USER_ROLE' })

  it('filters a three-item list, saves the pick and closes the menu', () => {
    const onSave = vi.fn()
    render(<SingleHarness onSave={onSave} />)
    expect(screen.getByRole('combobox', { name: 'Select DEFAULT_USER_ROLE' })).toHaveTextContent(
      'Select…',
    )

    openSingle()
    expect(searchSingle()).toHaveFocus()
    fireEvent.change(searchSingle(), { target: { value: 'ope' } })
    expect(options()).toEqual(['operator'])

    fireEvent.mouseDown(screen.getByRole('option', { name: 'operator' }))
    expect(onSave).toHaveBeenCalledWith('operator')
    // One choice: the menu is done and there is no chip to remove.
    expect(screen.queryByRole('listbox')).toBeNull()
    expect(screen.getByRole('combobox', { name: 'Select DEFAULT_USER_ROLE' })).toHaveTextContent(
      'operator',
    )
    expect(screen.queryByRole('button', { name: 'Remove operator' })).toBeNull()
  })

  it('cancels with Escape without saving', () => {
    const onSave = vi.fn()
    render(<SingleHarness onSave={onSave} />)

    openSingle()
    fireEvent.change(searchSingle(), { target: { value: 'viewer' } })
    // The prefix match ranks first ("reviewer" also contains the subsequence).
    expect(options()[0]).toBe('viewer')
    fireEvent.keyDown(searchSingle(), { key: 'Escape' })

    expect(screen.queryByRole('listbox')).toBeNull()
    expect(onSave).not.toHaveBeenCalled()
    expect(screen.getByRole('combobox', { name: 'Select DEFAULT_USER_ROLE' })).toHaveTextContent(
      'Select…',
    )
  })

  it('shows the label as the value and matches it when typing', () => {
    const onSave = vi.fn()
    const methods: SearchableMultiSelectOption[] = [
      { value: 'oauth', label: 'Atlassian OAuth' },
      { value: 'pat', label: 'PAT (Bearer)' },
    ]
    render(<SingleHarness options={methods} initial={['oauth']} onSave={onSave} />)
    expect(screen.getByRole('combobox', { name: 'Select DEFAULT_USER_ROLE' })).toHaveTextContent(
      'Atlassian OAuth',
    )

    // Matched on what the admin reads, and the pick still saves the machine id.
    openSingle()
    fireEvent.change(searchSingle(), { target: { value: 'bearer' } })
    expect(options()).toEqual(['PAT (Bearer)'])
    fireEvent.mouseDown(screen.getByRole('option', { name: 'PAT (Bearer)' }))
    expect(onSave).toHaveBeenCalledWith('pat')
  })
})

describe('SearchableMultiSelect', () => {
  it('opens with the search field focused and filters by case-insensitive subsequence', async () => {
    render(<Harness />)
    open()
    expect(search()).toHaveFocus()

    fireEvent.change(search(), { target: { value: 'API-GW' } })
    expect(options()).toEqual(['api-gateway'])

    fireEvent.change(search(), { target: { value: 'adm' } })
    expect(options()).toContain('admin')
  })

  it('ranks a prefix match first, then earlier matches', () => {
    render(<Harness />)
    open()
    fireEvent.change(search(), { target: { value: 'adm' } })
    // "admin" is a prefix; "badminton" matches at index 1.
    expect(options()).toEqual(['admin', 'badminton'])
  })

  it('selects the highlighted option with Enter and keeps the menu open for the next pick', () => {
    render(<Harness />)
    open()
    fireEvent.change(search(), { target: { value: 'gem' } })
    fireEvent.keyDown(search(), { key: 'Enter' })

    expect(screen.getByRole('button', { name: 'Remove gemini' })).toBeInTheDocument()
    // Still open, query cleared, so the next pick starts fresh.
    expect(search()).toHaveValue('')
    expect(screen.getByRole('listbox')).toBeInTheDocument()
    expect(screen.getAllByRole('option')).toHaveLength(OPTIONS.length)
    // The picked option is marked and picking it again removes it.
    expect(screen.getByRole('option', { name: /gemini/ })).toHaveAttribute('aria-selected', 'true')
  })

  it('moves the highlight with the arrow keys and closes on Escape', () => {
    render(<Harness />)
    open()
    fireEvent.keyDown(search(), { key: 'ArrowDown' })
    fireEvent.keyDown(search(), { key: 'ArrowDown' })
    fireEvent.keyDown(search(), { key: 'Enter' })
    expect(screen.getByRole('button', { name: 'Remove badminton' })).toBeInTheDocument()

    fireEvent.keyDown(search(), { key: 'Escape' })
    expect(screen.queryByRole('listbox')).toBeNull()
  })

  it('shows matched selected options marked and removes them from the chip when clicked again', () => {
    render(<Harness initial={['admin']} />)
    expect(screen.getByRole('button', { name: 'Remove admin' })).toBeInTheDocument()

    open()
    const admin = screen.getByRole('option', { name: 'admin' })
    expect(admin).toHaveAttribute('aria-selected', 'true')
    fireEvent.mouseDown(admin)
    expect(screen.queryByRole('button', { name: 'Remove admin' })).toBeNull()
  })

  it('keeps an unavailable option visible but not selectable, with its hint', () => {
    render(<Harness />)
    open()
    const slack = screen.getByRole('option', { name: /slack/ })
    expect(slack).toHaveAttribute('aria-disabled', 'true')
    expect(within(slack).getByText('not configured')).toBeInTheDocument()
    fireEvent.mouseDown(slack)
    expect(screen.queryByRole('button', { name: 'Remove slack' })).toBeNull()
  })

  it('offers free entry for a query that matches nothing exactly', () => {
    render(<Harness allowCustom />)
    open()
    fireEvent.change(search(), { target: { value: 'carol' } })
    // mouseDown is what the menu listens for (it keeps focus in the search field).
    fireEvent.mouseDown(screen.getByRole('option', { name: 'Add "carol"' }))
    expect(screen.getByRole('button', { name: 'Remove carol' })).toBeInTheDocument()

    // An exact match is a choice, not a free-entry row.
    fireEvent.change(search(), { target: { value: 'admin' } })
    expect(screen.queryByRole('option', { name: 'Add "admin"' })).toBeNull()
  })

  it('does not offer free entry unless allowed', () => {
    render(<Harness />)
    open()
    fireEvent.change(search(), { target: { value: 'carol' } })
    expect(screen.queryByRole('option', { name: 'Add "carol"' })).toBeNull()
    expect(screen.getByText('No matching choices')).toBeInTheDocument()
  })

  it('says the list is loading instead of showing it empty', () => {
    render(<Harness options={[]} loading />)
    open()
    expect(screen.getByText('Loading choices…')).toBeInTheDocument()
  })

  it('removes a value through its chip without opening the menu', async () => {
    render(<Harness initial={['admin', 'gemini']} />)
    await userEvent.click(screen.getByRole('button', { name: 'Remove admin' }))
    expect(screen.queryByRole('button', { name: 'Remove admin' })).toBeNull()
    expect(screen.getByRole('button', { name: 'Remove gemini' })).toBeInTheDocument()
    expect(screen.queryByRole('listbox')).toBeNull()
  })

  it('skips a disabled option with the arrow keys instead of stopping on it', () => {
    // A disabled row sits between two pickable ones, like an unconfigured exporter.
    render(
      <Harness
        options={[
          { value: 'viewer' },
          { value: 'reviewer', disabled: true, hint: 'not configured' },
          { value: 'operator' },
        ]}
      />,
    )
    open()

    fireEvent.keyDown(search(), { key: 'ArrowDown' })
    fireEvent.keyDown(search(), { key: 'Enter' })

    // One step past the disabled row, and it is the enabled one that got picked.
    expect(screen.getByRole('button', { name: 'Remove operator' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Remove reviewer' })).toBeNull()
  })

  it('never rests the highlight on a leading disabled option', () => {
    render(
      <Harness
        options={[
          { value: 'reviewer', disabled: true },
          { value: 'operator' },
        ]}
      />,
    )
    open()
    fireEvent.keyDown(search(), { key: 'Enter' })

    expect(screen.getByRole('button', { name: 'Remove operator' })).toBeInTheDocument()
  })
})

describe('SearchableMultiSelect disabled', () => {
  function DisabledHarness({ onSave }: { onSave: (values: string[]) => void }) {
    const [values, setValues] = useState<string[]>([])
    return (
      <SearchableMultiSelect
        label="ALLOWED_USERS"
        disabled
        values={values}
        options={OPTIONS}
        onChange={(next) => {
          setValues(next)
          onSave(next)
        }}
      />
    )
  }

  it('does not open on click, so nothing can be picked while the value saves', () => {
    const onSave = vi.fn()
    render(<DisabledHarness onSave={onSave} />)
    const trigger = screen.getByRole('combobox', { name: 'Add ALLOWED_USERS' })

    fireEvent.click(trigger)
    expect(screen.queryByRole('listbox')).toBeNull()
    expect(trigger).toHaveAttribute('aria-disabled', 'true')
    expect(trigger).toHaveAttribute('aria-expanded', 'false')
    expect(onSave).not.toHaveBeenCalled()
  })

  it('ignores Enter and the arrow keys on the trigger', () => {
    const onSave = vi.fn()
    render(<DisabledHarness onSave={onSave} />)
    const trigger = screen.getByRole('combobox', { name: 'Add ALLOWED_USERS' })

    fireEvent.keyDown(trigger, { key: 'Enter' })
    fireEvent.keyDown(trigger, { key: 'ArrowDown' })
    fireEvent.keyDown(trigger, { key: ' ' })

    expect(screen.queryByRole('listbox')).toBeNull()
    expect(onSave).not.toHaveBeenCalled()
  })
})
