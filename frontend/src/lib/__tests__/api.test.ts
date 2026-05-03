import { describe, it, expect, vi, beforeEach } from 'vitest'
import { api, ApiError, getRecentFailedCalls } from '../api'

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

  it('getRecentFailedCalls returns a copy', () => {
    const a = getRecentFailedCalls()
    const b = getRecentFailedCalls()
    expect(a).not.toBe(b)
    expect(a).toEqual(b)
  })
})
