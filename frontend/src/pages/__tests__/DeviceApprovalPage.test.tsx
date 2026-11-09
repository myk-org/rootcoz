import { render, screen, waitFor } from '@testing-library/react'
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

it('reports a code the server does not know', async () => {
  post.mockRejectedValue(new Error('That code is not valid'))
  renderPage()
  await typeCode('ZZZZ-ZZZZ')

  await userEvent.click(screen.getByRole('button', { name: /look up/i }))

  expect(await screen.findByText(/could not be approved/i)).toBeTruthy()
  // The lookup happened; nothing was approved.
  expect(post).toHaveBeenCalledTimes(1)
})
