import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { useEffect } from 'react'
import { CommentsSection, MENTION_RE } from '../CommentsSection'
import { ReportProvider, useReportDispatch } from '../ReportContext'
import type { Comment } from '@/types'

/* ------------------------------------------------------------------ */
/*  Mocks                                                              */
/* ------------------------------------------------------------------ */

const mockUsername = 'testuser'

vi.mock('@/lib/cookies', () => ({
  getUsername: () => mockUsername,
}))

const mockDelete = vi.fn()
const mockPost = vi.fn()
const mockGet = vi.fn()

vi.mock('@/lib/api', () => ({
  api: {
    get: (...args: unknown[]) => mockGet(...args),
    delete: (...args: unknown[]) => mockDelete(...args),
    post: (...args: unknown[]) => mockPost(...args),
  },
}))

/* ------------------------------------------------------------------ */
/*  Helpers                                                            */
/* ------------------------------------------------------------------ */

function makeComment(overrides: Partial<Comment> = {}): Comment {
  return {
    id: 1,
    job_id: 'job-1',
    test_name: 'test-a',
    child_job_name: '',
    child_build_number: 0,
    comment: 'A test comment',
    username: mockUsername,
    created_at: '2025-01-01T00:00:00Z',
    ...overrides,
  }
}

/**
 * Helper child that injects comments into ReportContext, then renders CommentsSection.
 */
function Injector({ comments }: { comments: Comment[] }) {
  const dispatch = useReportDispatch()
  useEffect(() => {
    dispatch({
      type: 'SET_COMMENTS_AND_REVIEWS',
      payload: { comments, reviews: {} },
    })
  }, []) // eslint-disable-line react-hooks/exhaustive-deps
  return (
    <CommentsSection
      jobId="job-1"
      testNames={['test-a']}
    />
  )
}

function renderWithComments(comments: Comment[]) {
  return render(
    <ReportProvider>
      <Injector comments={comments} />
    </ReportProvider>,
  )
}

/* ------------------------------------------------------------------ */
/*  Tests                                                              */
/* ------------------------------------------------------------------ */

beforeEach(async () => {
  vi.clearAllMocks()
  mockDelete.mockResolvedValue({})
  mockPost.mockResolvedValue({ enrichments: {} })
  mockGet.mockResolvedValue({ users: [] })
  // Reset mention cache between tests
  const { _resetMentionCache } = await import('../MentionTextarea')
  _resetMentionCache()
})

describe('CommentsSection – delete confirmation', () => {
  it('shows a confirmation dialog when the delete button is clicked', async () => {
    renderWithComments([makeComment()])

    const deleteBtn = screen.getByRole('button', { name: 'Delete comment' })
    fireEvent.click(deleteBtn)

    expect(screen.getByText('Are you sure you want to delete this comment? This action cannot be undone.')).toBeDefined()
    expect(screen.getByRole('button', { name: 'Delete' })).toBeDefined()
    expect(screen.getByRole('button', { name: 'Cancel' })).toBeDefined()
  })

  it('does not call the API when cancel is clicked', async () => {
    renderWithComments([makeComment()])

    fireEvent.click(screen.getByRole('button', { name: 'Delete comment' }))
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))

    expect(mockDelete).not.toHaveBeenCalled()
  })

  it('calls the delete API when confirm is clicked', async () => {
    renderWithComments([makeComment({ id: 42 })])

    fireEvent.click(screen.getByRole('button', { name: 'Delete comment' }))
    fireEvent.click(screen.getByRole('button', { name: 'Delete' }))

    await waitFor(() => {
      expect(mockDelete).toHaveBeenCalledWith('/results/job-1/comments/42')
    })
  })

  it('closes the dialog after successful deletion', async () => {
    renderWithComments([makeComment()])

    fireEvent.click(screen.getByRole('button', { name: 'Delete comment' }))
    fireEvent.click(screen.getByRole('button', { name: 'Delete' }))

    await waitFor(() => {
      expect(screen.queryByText('Are you sure you want to delete this comment? This action cannot be undone.')).toBeNull()
    })
  })

  it('does not show delete button for comments by other users', () => {
    renderWithComments([makeComment({ username: 'other-user' })])

    expect(screen.queryByRole('button', { name: 'Delete comment' })).toBeNull()
  })

  it('shows an error when deletion fails', async () => {
    mockDelete.mockRejectedValueOnce(new Error('Network error'))
    renderWithComments([makeComment()])

    fireEvent.click(screen.getByRole('button', { name: 'Delete comment' }))
    fireEvent.click(screen.getByRole('button', { name: 'Delete' }))

    await waitFor(() => {
      expect(screen.getByRole('alert')).toBeDefined()
      expect(screen.getByText('Network error')).toBeDefined()
    })
  })
})

describe('CommentsSection – @mention highlighting', () => {
  it('highlights @mentions in rendered comments', () => {
    renderWithComments([makeComment({ comment: 'Hey @alice check this' })])
    const mention = screen.getByText('@alice')
    expect(mention.tagName).toBe('SPAN')
    expect(mention.className).toContain('text-signal-blue')
    expect(mention.className).toContain('font-semibold')
  })

  it('does not highlight @domain in email addresses as a mention', () => {
    renderWithComments([makeComment({ comment: 'Contact user@domain.com for info' })])
    const mentionSpans = document.querySelectorAll('.text-signal-blue.font-semibold')
    mentionSpans.forEach((el) => {
      expect(el.textContent).not.toBe('@domain')
    })
  })

  it('renders plain text without @mention styling', () => {
    renderWithComments([makeComment({ comment: 'No mentions here' })])
    expect(screen.getByText('No mentions here')).toBeDefined()
    expect(screen.queryByText(/@/)).toBeNull()
  })
})

/* ------------------------------------------------------------------ */
/*  Mention regex parity with Python backend                           */
/* ------------------------------------------------------------------ */
// These test cases are shared with Python tests/test_mentions.py::TestMentionRegexParity
// If you change these, update the Python side too.

/** Extract mention usernames from text using MENTION_RE (same as backend). */
function extractMentions(text: string): string[] {
  MENTION_RE.lastIndex = 0
  const seen = new Set<string>()
  const mentions: string[] = []
  let m: RegExpExecArray | null
  while ((m = MENTION_RE.exec(text)) !== null) {
    if (!seen.has(m[1])) {
      seen.add(m[1])
      mentions.push(m[1])
    }
  }
  return mentions
}

// Shared mention regex test cases — MUST match Python's _MENTION_PATTERN in comment_enrichment.py
// Pattern: (?<![a-zA-Z0-9.])@([a-zA-Z0-9_-]+)
const MENTION_TEST_CASES = [
  { input: 'hello @alice', expected: ['alice'] },
  { input: '@bob test', expected: ['bob'] },
  { input: 'cc @alice @bob', expected: ['alice', 'bob'] },
  { input: 'email user@domain.com', expected: [] },          // email — no match
  { input: 'no mentions here', expected: [] },
  { input: '@alice-bob', expected: ['alice-bob'] },           // hyphens allowed
  { input: '@alice_bob', expected: ['alice_bob'] },           // underscores allowed
  { input: '@alice123', expected: ['alice123'] },             // digits allowed
  { input: '.@alice', expected: [] },                          // preceded by dot — no match
  { input: 'x@alice', expected: [] },                          // preceded by letter — no match
  { input: '1@alice', expected: [] },                          // preceded by digit — no match
  { input: '(@alice)', expected: ['alice'] },                  // parens ok
  { input: '@alice.', expected: ['alice'] },                   // trailing dot ok
  { input: '@alice ping @alice', expected: ['alice'] },        // deduplication — same user only once
]

describe('MENTION_RE – parity with Python _MENTION_PATTERN', () => {
  it.each(MENTION_TEST_CASES)(
    'extracts $expected from "$input"',
    ({ input, expected }) => {
      expect(extractMentions(input)).toEqual(expected)
    },
  )
})
