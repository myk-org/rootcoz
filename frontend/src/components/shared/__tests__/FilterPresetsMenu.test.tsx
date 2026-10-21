import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { FilterPresetsMenu } from '../FilterPresetsMenu'

// ─── Mocks ──────────────────────────────────────────────────────────

const mockAuth = {
  isOperator: false,
}

vi.mock('@/lib/auth', () => ({
  useAuth: () => mockAuth,
}))

const apiGet = vi.fn()
const apiPost = vi.fn()
const apiPut = vi.fn()
const apiDelete = vi.fn()

vi.mock('@/lib/api', () => ({
  api: {
    get: (...args: unknown[]) => apiGet(...args),
    post: (...args: unknown[]) => apiPost(...args),
    put: (...args: unknown[]) => apiPut(...args),
    delete: (...args: unknown[]) => apiDelete(...args),
  },
  extractApiDetail: (err: unknown) =>
    err instanceof Error ? err.message : 'request failed',
}))

// ─── Fixtures ───────────────────────────────────────────────────────

const PERSONAL_PRESET = {
  id: 1,
  name: 'Storage unreviewed',
  page: 'dashboard',
  scope: 'user' as const,
  filters: { team: ['storage'], review_status: 'unreviewed' },
  created_at: '2026-01-01T00:00:00',
  updated_at: '2026-01-01T00:00:00',
}

const SERVER_PRESET = {
  id: 2,
  name: 'Team view',
  page: 'dashboard',
  scope: 'server' as const,
  filters: { tier: ['2'] },
  created_at: '2026-01-02T00:00:00',
  updated_at: '2026-01-02T00:00:00',
}

function params(entries: Record<string, string | string[]>): URLSearchParams {
  const p = new URLSearchParams()
  for (const [key, value] of Object.entries(entries)) {
    for (const v of Array.isArray(value) ? value : [value]) p.append(key, v)
  }
  return p
}

function renderMenu(searchParams: URLSearchParams = new URLSearchParams()) {
  const onApply = vi.fn()
  const utils = render(
    <FilterPresetsMenu page="dashboard" searchParams={searchParams} onApply={onApply} />,
  )
  return { ...utils, onApply }
}

const user = userEvent.setup()

async function openMenu() {
  await user.click(screen.getByTestId('filter-presets-trigger'))
}

// ─── Tests ──────────────────────────────────────────────────────────

describe('FilterPresetsMenu', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mockAuth.isOperator = false
    apiGet.mockResolvedValue([PERSONAL_PRESET, SERVER_PRESET])
  })

  it('fetches presets for its page on mount', async () => {
    renderMenu()
    await waitFor(() => expect(apiGet).toHaveBeenCalledWith('/api/user/filter-presets?page=dashboard'))
  })

  it('lists presets in the dropdown', async () => {
    renderMenu()
    await waitFor(() => expect(apiGet).toHaveBeenCalled())
    await openMenu()
    expect(screen.getByText('Storage unreviewed')).toBeDefined()
    expect(screen.getByText('Team view')).toBeDefined()
  })

  it('shows the empty state when no presets exist', async () => {
    apiGet.mockResolvedValue([])
    renderMenu()
    await waitFor(() => expect(apiGet).toHaveBeenCalled())
    await openMenu()
    expect(screen.getByText(/No presets yet/)).toBeDefined()
  })

  it('applies a preset on click', async () => {
    const { onApply } = renderMenu()
    await waitFor(() => expect(apiGet).toHaveBeenCalled())
    await openMenu()
    await user.click(screen.getByText('Storage unreviewed'))
    expect(onApply).toHaveBeenCalledWith(PERSONAL_PRESET.filters)
  })

  it('shows the active preset name in the trigger and a modified dot when filters diverge', async () => {
    // Preset is active but the URL now carries an extra filter
    const { rerender } = renderMenu(
      params({ team: ['storage'], review_status: 'unreviewed', tier: ['2'] }),
    )
    await waitFor(() => expect(apiGet).toHaveBeenCalled())
    await openMenu()
    await user.click(screen.getByText('Storage unreviewed'))
    // Trigger now shows the preset name
    expect(screen.getByTestId('filter-presets-trigger').textContent).toContain('Storage unreviewed')
    // Filters diverge (tier added) → modified dot + Update button
    expect(screen.getByTestId('filter-preset-modified-dot')).toBeDefined()
    expect(screen.getByTestId('filter-preset-update')).toBeDefined()
    // Unload button appears
    expect(screen.getByTestId('filter-preset-unload')).toBeDefined()
    // No modified dot when filters match the preset
    rerender(
      <FilterPresetsMenu
        page="dashboard"
        searchParams={params({ team: ['storage'], review_status: 'unreviewed' })}
        onApply={vi.fn()}
      />,
    )
    expect(screen.queryByTestId('filter-preset-modified-dot')).toBeNull()
    expect(screen.queryByTestId('filter-preset-update')).toBeNull()
  })

  it('unloads the preset (keeps filters) on ✕', async () => {
    renderMenu(params({ team: ['storage'], review_status: 'unreviewed' }))
    await waitFor(() => expect(apiGet).toHaveBeenCalled())
    await openMenu()
    await user.click(screen.getByText('Storage unreviewed'))
    expect(screen.getByTestId('filter-presets-trigger').textContent).toContain('Storage unreviewed')
    await user.click(screen.getByTestId('filter-preset-unload'))
    expect(screen.getByTestId('filter-presets-trigger').textContent).toContain('Presets')
  })

  it('updates the active preset with the current filters via Update', async () => {
    apiPut.mockResolvedValue(PERSONAL_PRESET)
    const filters = { team: 'storage', review_status: 'unreviewed', tier: '2' }
    renderMenu(params(filters))
    await waitFor(() => expect(apiGet).toHaveBeenCalled())
    await openMenu()
    await user.click(screen.getByText('Storage unreviewed'))
    await user.click(screen.getByTestId('filter-preset-update'))
    await waitFor(() =>
      expect(apiPut).toHaveBeenCalledWith('/api/user/filter-presets/1', { filters }),
    )
  })

  it('saves the current filters with a name and scope', async () => {
    apiPost.mockResolvedValue(PERSONAL_PRESET)
    const filters = { team: 'storage' }
    renderMenu(params(filters))
    await waitFor(() => expect(apiGet).toHaveBeenCalled())
    await openMenu()
    await user.click(screen.getByTestId('filter-preset-save-as'))
    fireEvent.change(screen.getByTestId('filter-preset-save-name'), {
      target: { value: 'My preset' },
    })
    await user.click(screen.getByTestId('filter-preset-save-confirm'))
    await waitFor(() =>
      expect(apiPost).toHaveBeenCalledWith('/api/user/filter-presets', {
        name: 'My preset',
        page: 'dashboard',
        scope: 'user',
        filters,
      }),
    )
  })

  it('shows the save error on conflict', async () => {
    apiPost.mockRejectedValue(new Error('Preset name already exists for this page'))
    renderMenu(params({ team: ['storage'] }))
    await waitFor(() => expect(apiGet).toHaveBeenCalled())
    await openMenu()
    await user.click(screen.getByTestId('filter-preset-save-as'))
    fireEvent.change(screen.getByTestId('filter-preset-save-name'), {
      target: { value: 'Dup' },
    })
    await user.click(screen.getByTestId('filter-preset-save-confirm'))
    expect(
      await screen.findByTestId('filter-preset-save-error'),
    ).toHaveTextContent('Preset name already exists')
  })

  it('disables the server scope toggle for non-operators', async () => {
    renderMenu(params({ team: ['storage'] }))
    await waitFor(() => expect(apiGet).toHaveBeenCalled())
    await openMenu()
    await user.click(screen.getByTestId('filter-preset-save-as'))
    const serverButton = screen.getByRole('button', { name: 'Server filter' })
    expect(serverButton).toHaveProperty('disabled', true)
  })

  it('offers the server scope to operators', async () => {
    mockAuth.isOperator = true
    apiPost.mockResolvedValue(SERVER_PRESET)
    const filters = { tier: '2' }
    renderMenu(params(filters))
    await waitFor(() => expect(apiGet).toHaveBeenCalled())
    await openMenu()
    await user.click(screen.getByTestId('filter-preset-save-as'))
    await user.click(screen.getByRole('button', { name: 'Server filter' }))
    fireEvent.change(screen.getByTestId('filter-preset-save-name'), {
      target: { value: 'Team view' },
    })
    await user.click(screen.getByTestId('filter-preset-save-confirm'))
    await waitFor(() =>
      expect(apiPost).toHaveBeenCalledWith('/api/user/filter-presets', {
        name: 'Team view',
        page: 'dashboard',
        scope: 'server',
        filters,
      }),
    )
  })

  it('hides rename/delete actions for server presets when not operator', async () => {
    renderMenu()
    await waitFor(() => expect(apiGet).toHaveBeenCalled())
    await openMenu()
    await user.click(screen.getByText('Team view'))
    // No rename/delete buttons rendered for the server row
    expect(screen.queryByLabelText('Rename Team view')).toBeNull()
    expect(screen.queryByLabelText('Delete Team view')).toBeNull()
  })

  it('renames a preset from the row action', async () => {
    apiPut.mockResolvedValue({ ...PERSONAL_PRESET, name: 'Renamed' })
    renderMenu()
    await waitFor(() => expect(apiGet).toHaveBeenCalled())
    await openMenu()
    await user.click(screen.getByLabelText('Rename Storage unreviewed'))
    fireEvent.change(screen.getByTestId('filter-preset-rename-name'), {
      target: { value: 'Renamed' },
    })
    await user.click(screen.getByRole('button', { name: 'Rename' }))
    await waitFor(() =>
      expect(apiPut).toHaveBeenCalledWith('/api/user/filter-presets/1', { name: 'Renamed' }),
    )
  })

  it('deletes a preset after confirmation', async () => {
    apiDelete.mockResolvedValue(undefined)
    renderMenu()
    await waitFor(() => expect(apiGet).toHaveBeenCalled())
    await openMenu()
    await user.click(screen.getByLabelText('Delete Storage unreviewed'))
    await user.click(screen.getByRole('button', { name: 'Delete' }))
    await waitFor(() => expect(apiDelete).toHaveBeenCalledWith('/api/user/filter-presets/1'))
  })
})
