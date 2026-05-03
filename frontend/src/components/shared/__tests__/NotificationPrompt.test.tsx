import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, act, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { NotificationPrompt } from '../NotificationPrompt'

// Mock the notifications module
vi.mock('@/lib/notifications', () => ({
  getPushSubscriptionState: vi.fn(),
  getVapidPublicKey: vi.fn(),
  subscribeToPush: vi.fn(),
  hasActivePushSubscription: vi.fn(),
}))

import { getPushSubscriptionState, getVapidPublicKey, subscribeToPush, hasActivePushSubscription } from '@/lib/notifications'

const mockGetState = vi.mocked(getPushSubscriptionState)
const mockGetVapid = vi.mocked(getVapidPublicKey)
const mockSubscribe = vi.mocked(subscribeToPush)
const mockHasActiveSub = vi.mocked(hasActivePushSubscription)

const ASKED_KEY = 'jji_notifications_asked'

describe('NotificationPrompt', () => {
  beforeEach(() => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    localStorage.clear()
    mockGetState.mockResolvedValue('default')
    mockGetVapid.mockResolvedValue('fake-vapid-key')
    mockSubscribe.mockResolvedValue({ ok: true })
    mockHasActiveSub.mockResolvedValue(false)
  })

  afterEach(() => {
    vi.useRealTimers()
    vi.clearAllMocks()
  })

  it('shows dialog after delay when all conditions are met', async () => {
    render(<NotificationPrompt />)

    // Not visible immediately
    expect(screen.queryByText('Enable Notifications?')).toBeNull()

    // Flush async checks + timer
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2000)
    })

    expect(screen.getByText('Enable Notifications?')).toBeDefined()
    expect(screen.getByText('Get notified when someone mentions you in a comment.')).toBeDefined()
    expect(screen.getByRole('button', { name: 'Enable' })).toBeDefined()
    expect(screen.getByRole('button', { name: 'Not now' })).toBeDefined()
  })

  it('does not show dialog if already asked', async () => {
    localStorage.setItem(ASKED_KEY, 'true')

    render(<NotificationPrompt />)

    await act(async () => {
      await vi.advanceTimersByTimeAsync(2000)
    })

    expect(screen.queryByText('Enable Notifications?')).toBeNull()
  })

  it('does not show dialog if push is unsupported', async () => {
    mockGetState.mockResolvedValue('unsupported')

    render(<NotificationPrompt />)

    await act(async () => {
      await vi.advanceTimersByTimeAsync(2000)
    })

    expect(screen.queryByText('Enable Notifications?')).toBeNull()
  })

  it('does not show dialog if granted with active subscription', async () => {
    mockGetState.mockResolvedValue('granted')
    mockHasActiveSub.mockResolvedValue(true)

    render(<NotificationPrompt />)

    await act(async () => {
      await vi.advanceTimersByTimeAsync(2000)
    })

    expect(screen.queryByText('Enable Notifications?')).toBeNull()
    // Should mark as asked so it never checks again
    expect(localStorage.getItem(ASKED_KEY)).toBe('true')
  })

  it('shows dialog if granted but no active subscription', async () => {
    mockGetState.mockResolvedValue('granted')
    mockHasActiveSub.mockResolvedValue(false)

    render(<NotificationPrompt />)

    await act(async () => {
      await vi.advanceTimersByTimeAsync(2000)
    })

    expect(screen.getByText('Enable Notifications?')).toBeDefined()
  })

  it('does not show dialog if VAPID key unavailable', async () => {
    mockGetVapid.mockResolvedValue(null)

    render(<NotificationPrompt />)

    await act(async () => {
      await vi.advanceTimersByTimeAsync(2000)
    })

    expect(screen.queryByText('Enable Notifications?')).toBeNull()
  })

  it('calls subscribeToPush and marks asked when Enable clicked', async () => {
    vi.useRealTimers()
    render(<NotificationPrompt />)

    await waitFor(() => {
      expect(screen.getByText('Enable Notifications?')).toBeDefined()
    }, { timeout: 3000 })

    const enableBtn = screen.getByRole('button', { name: 'Enable' })
    await userEvent.click(enableBtn)

    await waitFor(() => {
      expect(mockSubscribe).toHaveBeenCalledOnce()
      expect(localStorage.getItem(ASKED_KEY)).toBe('true')
      expect(screen.queryByText('Enable Notifications?')).toBeNull()
    })
  })

  it('marks asked without subscribing when Not now clicked', async () => {
    vi.useRealTimers()
    render(<NotificationPrompt />)

    await waitFor(() => {
      expect(screen.getByText('Enable Notifications?')).toBeDefined()
    }, { timeout: 3000 })

    const dismissBtn = screen.getByRole('button', { name: 'Not now' })
    await userEvent.click(dismissBtn)

    await waitFor(() => {
      expect(mockSubscribe).not.toHaveBeenCalled()
      expect(localStorage.getItem(ASKED_KEY)).toBe('true')
      expect(screen.queryByText('Enable Notifications?')).toBeNull()
    })
  })
})
