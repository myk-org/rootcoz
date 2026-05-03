import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { MentionTextarea, _resetMentionCache } from '../MentionTextarea'

/* ------------------------------------------------------------------ */
/*  Mocks                                                              */
/* ------------------------------------------------------------------ */

const mockGet = vi.fn()

vi.mock('@/lib/api', () => ({
  api: {
    get: (...args: unknown[]) => mockGet(...args),
  },
}))

/* ------------------------------------------------------------------ */
/*  Helpers                                                            */
/* ------------------------------------------------------------------ */

function renderTextarea(overrides: Partial<Parameters<typeof MentionTextarea>[0]> = {}) {
  const props = {
    value: '',
    onChange: vi.fn(),
    onSubmit: vi.fn(),
    placeholder: 'Add a comment...',
    ...overrides,
  }
  const result = render(<MentionTextarea {...props} />)
  return { ...result, ...props }
}

/* ------------------------------------------------------------------ */
/*  Tests                                                              */
/* ------------------------------------------------------------------ */

beforeEach(() => {
  vi.clearAllMocks()
  _resetMentionCache()
  mockGet.mockResolvedValue({ usernames: ['alice', 'bob', 'charlie'] })
})

describe('MentionTextarea', () => {
  it('renders a textarea with the given placeholder', () => {
    renderTextarea()
    expect(screen.getByPlaceholderText('Add a comment...')).toBeDefined()
  })

  it('calls onChange when text is typed', () => {
    const { onChange } = renderTextarea()
    const textarea = screen.getByRole('textbox')
    fireEvent.change(textarea, { target: { value: 'hello' } })
    expect(onChange).toHaveBeenCalledWith('hello')
  })

  it('calls onSubmit when Enter is pressed without Shift', () => {
    const { onSubmit } = renderTextarea({ value: 'some text' })
    const textarea = screen.getByRole('textbox')
    fireEvent.keyDown(textarea, { key: 'Enter', shiftKey: false })
    expect(onSubmit).toHaveBeenCalled()
  })

  it('does not call onSubmit when Shift+Enter is pressed', () => {
    const { onSubmit } = renderTextarea({ value: 'some text' })
    const textarea = screen.getByRole('textbox')
    fireEvent.keyDown(textarea, { key: 'Enter', shiftKey: true })
    expect(onSubmit).not.toHaveBeenCalled()
  })

  it('fetches mentionable users on mount', async () => {
    renderTextarea()
    await waitFor(() => {
      expect(mockGet).toHaveBeenCalledWith('/api/users/mentionable')
    })
  })

  it('caches users and does not re-fetch', async () => {
    const { unmount } = renderTextarea()
    await waitFor(() => {
      expect(mockGet).toHaveBeenCalledTimes(1)
    })
    unmount()
    renderTextarea()
    // Should not call again — cached
    expect(mockGet).toHaveBeenCalledTimes(1)
  })

  it('shows dropdown when @ is typed and users match', async () => {
    renderTextarea({ value: '@a' })
    const textarea = screen.getByRole('textbox')

    // Wait for users to be fetched
    await waitFor(() => {
      expect(mockGet).toHaveBeenCalled()
    })

    // Simulate cursor at end of value
    Object.defineProperty(textarea, 'selectionStart', { value: 2, writable: true })
    fireEvent.select(textarea)

    await waitFor(() => {
      expect(screen.getByRole('listbox')).toBeDefined()
      expect(screen.getByText('@alice')).toBeDefined()
    })
  })

  it('does not show dropdown when no users match', async () => {
    renderTextarea({ value: '@zzz' })
    const textarea = screen.getByRole('textbox')

    await waitFor(() => {
      expect(mockGet).toHaveBeenCalled()
    })

    Object.defineProperty(textarea, 'selectionStart', { value: 4, writable: true })
    fireEvent.select(textarea)

    // Should not show listbox
    expect(screen.queryByRole('listbox')).toBeNull()
  })

  it('closes dropdown on Escape', async () => {
    renderTextarea({ value: '@a' })
    const textarea = screen.getByRole('textbox')

    await waitFor(() => {
      expect(mockGet).toHaveBeenCalled()
    })

    Object.defineProperty(textarea, 'selectionStart', { value: 2, writable: true })
    fireEvent.select(textarea)

    await waitFor(() => {
      expect(screen.getByRole('listbox')).toBeDefined()
    })

    fireEvent.keyDown(textarea, { key: 'Escape' })
    expect(screen.queryByRole('listbox')).toBeNull()
  })
})
