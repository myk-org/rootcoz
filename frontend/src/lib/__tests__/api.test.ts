import { describe, it, expect, vi, beforeEach } from 'vitest'
import { api, ApiError, getRecentFailedCalls } from '../api'
import { getRecentErrors } from '../errorCapture'

describe('api', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
  })

  it('returns parsed JSON on success', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ status: 'ok' }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }),
    )
    const result = await api.get<{ status: string }>('/test')
    expect(result.status).toBe('ok')
  })

  it('throws ApiError on non-ok response', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ detail: 'not found' }), {
        status: 404,
        statusText: 'Not Found',
        headers: { 'Content-Type': 'application/json' },
      }),
    )
    await expect(api.get('/missing')).rejects.toThrow(ApiError)
  })

  it('returns undefined for 204 No Content', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(null, { status: 204 }),
    )
    const result = await api.delete('/item')
    expect(result).toBeUndefined()
  })

  it('post sends JSON body', async () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ id: 1 }), {
        status: 201,
        headers: { 'Content-Type': 'application/json' },
      }),
    )
    await api.post('/create', { name: 'test' })
    const [, options] = fetchSpy.mock.calls[0]
    expect(options?.method).toBe('POST')
    expect(options?.body).toBe(JSON.stringify({ name: 'test' }))
  })

  it('tracks failed API calls (status >= 400)', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ detail: 'bad request' }), {
        status: 400,
        statusText: 'Bad Request',
        headers: { 'Content-Type': 'application/json' },
      }),
    )
    const beforeCount = getRecentFailedCalls().length
    await expect(api.get('/bad-endpoint')).rejects.toThrow(ApiError)
    const after = getRecentFailedCalls()
    expect(after.length).toBe(beforeCount + 1)
    const last = after[after.length - 1]
    expect(last.status).toBe(400)
    expect(last.endpoint).toBe('/bad-endpoint')
    expect(last.timestamp).toBeGreaterThan(0)
  })

  it('does not retain query parameters from failed requests', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ detail: 'bad request' }), {
        status: 400,
        statusText: 'Bad Request',
        headers: { 'Content-Type': 'application/json' },
      }),
    )
    await expect(
      api.post('/push?subject_identifier=build&waiver_comment=private'),
    ).rejects.toThrow(ApiError)
    const calls = getRecentFailedCalls()
    expect(calls[calls.length - 1].endpoint).toBe('/push')
  })

  it('uses a fixed placeholder when endpoint normalization fails', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ detail: 'private response data' }), {
        status: 400,
        statusText: 'Bad Request',
        headers: { 'Content-Type': 'application/json' },
      }),
    )
    const credentials = 'private-user:private-password'
    const malformedPath = '[invalid-host/private-path'
    const privateQuery =
      'subject_identifier=private-subject&waiver_comment=private-comment'
    const malformedEndpoint =
      `https://${credentials}@${malformedPath}?${privateQuery}`
    await expect(api.post(malformedEndpoint)).rejects.toThrow(ApiError)

    const errors = getRecentErrors()
    expect(errors[errors.length - 1]).toBe('Failed to normalize API endpoint')
    const calls = getRecentFailedCalls()
    const capturedEndpoint = calls[calls.length - 1].endpoint
    expect(capturedEndpoint).toBe('<invalid-endpoint>')
    for (const sensitiveValue of [
      malformedEndpoint,
      credentials,
      malformedPath,
      privateQuery,
    ]) {
      expect(capturedEndpoint).not.toContain(sensitiveValue)
    }
  })

  it('getRecentFailedCalls returns a copy', () => {
    const a = getRecentFailedCalls()
    const b = getRecentFailedCalls()
    expect(a).not.toBe(b)
    expect(a).toEqual(b)
  })
})
