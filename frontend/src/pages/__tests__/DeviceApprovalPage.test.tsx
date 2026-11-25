import { act, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, expect, it, vi } from 'vitest'
import { DeviceApprovalPage } from '@/pages/DeviceApprovalPage'

vi.mock('@/lib/auth', () => ({
  useAuth: () => ({ username: 'alice' }),
}))

const post = vi.fn()
vi.mock('@/lib/api', async (importActual) => ({
  ...(await importActual<typeof import('@/lib/api')>()),
  api: {
    post: (...args: unknown[]) => post(...args),
  },
}))

function renderPage() {
  return render(
    <MemoryRouter initialEntries={['/login/device']}>
      <DeviceApprovalPage />
    </MemoryRouter>,
  )
}

async function typeCode(code = 'ABCD-EFGH') {
  await userEvent.type(screen.getByLabelText(/device code/i), code)
}

beforeEach(() => {
  post.mockReset()
})

it('names the client that is asking before anything is approved', async () => {
  post.mockResolvedValue({
    user_code: 'ABCD-EFGH',
    provider: 'google',
    client_name: 'laptop',
    expires_at: '2030-01-01 00:00:00',
    authorized: false,
  })
  renderPage()
  await typeCode()

  await userEvent.click(screen.getByRole('button', { name: /look up/i }))

  expect(await screen.findByText(/laptop/)).toBeTruthy()
  // The account being authorised, not the one-time code, is the subject.
  expect(screen.getByText('alice')).toBeTruthy()
  expect(post).toHaveBeenCalledWith('/api/auth/sso/device/approve/lookup', {
    user_code: 'ABCD-EFGH',
  })
})

it('approves the code the person is looking at', async () => {
  post.mockResolvedValue({
    user_code: 'ABCD-EFGH',
    provider: null,
    client_name: null,
    expires_at: '2030-01-01 00:00:00',
    authorized: false,
  })
  renderPage()
  await typeCode()

  await userEvent.click(screen.getByRole('button', { name: /look up/i }))
  await userEvent.click(
    await screen.findByRole('button', { name: /approve sign-in/i }),
  )

  await waitFor(() =>
    expect(post).toHaveBeenCalledWith('/api/auth/sso/device/approve', {
      user_code: 'ABCD-EFGH',
    }),
  )
  expect(await screen.findByText(/Approved\./)).toBeTruthy()
})

it('ignores a code supplied in the query string', async () => {
  // A live code in a URL is written to the access log (and, via the login
  // return path, into the SSO `next` parameter), so the field is never filled
  // from one — pin that, or a regression would pass silently.
  render(
    <MemoryRouter initialEntries={['/login/device?user_code=ABCD-EFGH']}>
      <DeviceApprovalPage />
    </MemoryRouter>,
  )

  expect((screen.getByLabelText(/device code/i) as HTMLInputElement).value).toBe('')
})

it('approves the code whose client name was shown, not a later edit', async () => {
  post.mockResolvedValue({
    user_code: 'ABCD-EFGH',
    provider: 'google',
    client_name: 'laptop',
    expires_at: '2030-01-01 00:00:00',
    authorized: false,
  })
  renderPage()
  await typeCode()
  await userEvent.click(screen.getByRole('button', { name: /look up/i }))
  await screen.findByText(/laptop/)

  // Retyping what will be approved hides the description it belonged to.
  await userEvent.type(screen.getByLabelText(/device code/i), 'X')

  expect(screen.queryByRole('button', { name: /approve sign-in/i })).toBeNull()
})

it('never offers to approve a code that was already approved', async () => {
  post.mockResolvedValue({
    user_code: 'ABCD-EFGH',
    provider: 'google',
    client_name: 'laptop',
    expires_at: '2030-01-01 00:00:00',
    authorized: true,
  })
  renderPage()
  await typeCode()

  await userEvent.click(screen.getByRole('button', { name: /look up/i }))

  expect(await screen.findByText(/already approved/i)).toBeTruthy()
  expect(screen.queryByRole('button', { name: /approve sign-in/i })).toBeNull()
})

it('keeps the last submitted code when lookups answer out of order', async () => {
  // Two lookups can overlap; the earlier one answering last must not describe
  // (and let the person approve) a code they have already replaced.
  const pending: Array<(value: unknown) => void> = []
  post.mockImplementation((url: string) =>
    url === '/api/auth/sso/device/approve'
      ? Promise.resolve({})
      : new Promise((resolve) => {
          pending.push(resolve)
        }),
  )
  renderPage()

  await typeCode('AAAA-AAAA')
  await userEvent.click(screen.getByRole('button', { name: /look up/i }))
  await userEvent.clear(screen.getByLabelText(/device code/i))
  await typeCode('BBBB-BBBB')
  await userEvent.click(screen.getByRole('button', { name: /look up/i }))

  await act(async () => {
    pending[1]({
      user_code: 'BBBB-BBBB',
      provider: null,
      client_name: 'later-laptop',
      expires_at: '2030-01-01 00:00:00',
      authorized: false,
    })
  })
  expect(screen.getByText(/later-laptop/)).toBeTruthy()

  await act(async () => {
    pending[0]({
      user_code: 'AAAA-AAAA',
      provider: null,
      client_name: 'earlier-laptop',
      expires_at: '2030-01-01 00:00:00',
      authorized: false,
    })
  })
  expect(screen.queryByText(/earlier-laptop/)).toBeNull()
  expect(screen.getByText(/BBBB-BBBB/)).toBeTruthy()

  await userEvent.click(screen.getByRole('button', { name: /approve sign-in/i }))
  await waitFor(() =>
    expect(post).toHaveBeenCalledWith('/api/auth/sso/device/approve', {
      user_code: 'BBBB-BBBB',
    }),
  )
})

it('does not claim approval when a newer lookup landed during the approve flight', async () => {
  // Approve code A is in flight; a lookup for code B resolves before the
  // approve completes.  The screen now describes B, so the component must
  // NOT announce A as approved — it surfaces a notice instead.
  let resolveApprove!: () => void
  post.mockImplementation((url: string) => {
    if (url === '/api/auth/sso/device/approve') {
      return new Promise<void>((r) => {
        resolveApprove = r
      })
    }
    // lookup — resolve immediately with appropriate code
    const code = post.mock.calls.at(-1)?.[1]?.user_code ?? 'AAAA-AAAA'
    return Promise.resolve({
      user_code: code,
      provider: null,
      client_name: code === 'AAAA-AAAA' ? 'laptop-a' : 'laptop-b',
      expires_at: '2030-01-01 00:00:00',
      authorized: false,
    })
  })

  renderPage()

  // Look up and start approving code A
  await typeCode('AAAA-AAAA')
  await userEvent.click(screen.getByRole('button', { name: /look up/i }))
  await screen.findByText(/laptop-a/)
  await userEvent.click(
    screen.getByRole('button', { name: /approve sign-in/i }),
  )

  // While approve is in flight, type a new code and look it up
  await userEvent.clear(screen.getByLabelText(/device code/i))
  await typeCode('BBBB-BBBB')
  await userEvent.click(screen.getByRole('button', { name: /look up/i }))
  await screen.findByText(/laptop-b/)

  // Now let the original approve resolve
  await act(async () => {
    resolveApprove()
  })

  // Must NOT show the "Approved." banner
  expect(screen.queryByText(/Approved\./)).toBeNull()
  // Must surface a notice about the code having changed
  expect(screen.getByText(/displayed code changed/i)).toBeTruthy()
  // The approve button for the new code should still be available
  expect(
    screen.getByRole('button', { name: /approve sign-in/i }),
  ).toBeTruthy()
})

it('reports a code the server does not know', async () => {
  post.mockRejectedValue(new Error('That code is not valid'))
  renderPage()
  await typeCode('ZZZZ-ZZZZ')

  await userEvent.click(screen.getByRole('button', { name: /look up/i }))

  expect(await screen.findByText(/could not be approved/i)).toBeTruthy()
  // The lookup happened; nothing was approved.
  expect(post).toHaveBeenCalledTimes(1)
})
