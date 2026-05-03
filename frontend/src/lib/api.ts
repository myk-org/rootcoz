/** Centralized fetch wrapper for the JJI API. */

// -- Failed API call tracking ------------------------------------

interface FailedApiCall {
  status: number
  endpoint: string
  error: string
  timestamp: number
}

const MAX_FAILED_CALLS = 10
const recentFailedCalls: FailedApiCall[] = []

function trackFailedCall(entry: FailedApiCall) {
  if (recentFailedCalls.length >= MAX_FAILED_CALLS) {
    recentFailedCalls.shift()
  }
  recentFailedCalls.push(entry)
}

/** Return a snapshot of the recent failed API calls (status >= 400). */
export function getRecentFailedCalls(): FailedApiCall[] {
  return [...recentFailedCalls]
}

// ----------------------------------------------------------------

class ApiError extends Error {
  status: number
  statusText: string
  body: unknown

  constructor(status: number, statusText: string, body: unknown) {
    super(`API error ${status}: ${statusText}`)
    this.name = 'ApiError'
    this.status = status
    this.statusText = statusText
    this.body = body
  }
}

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const res = await fetch(path, {
    ...options,
    headers: {
      'Content-Type': 'application/json',
      ...options?.headers,
    },
  })

  if (!res.ok) {
    let body: unknown
    try {
      const text = await res.text()
      try {
        body = JSON.parse(text)
      } catch {
        body = text
      }
    } catch {
      body = null
    }
    trackFailedCall({
      status: res.status,
      endpoint: path,
      error: typeof body === 'string' ? body : body != null ? JSON.stringify(body) : '',
      timestamp: Date.now(),
    })
    throw new ApiError(res.status, res.statusText, body)
  }

  // 204 No Content — intentional cast; callers (e.g. api.delete) do not use the return value.
  if (res.status === 204) return undefined as T

  return res.json()
}

const requestWithJsonBody = <T>(
  method: 'POST' | 'PUT' | 'DELETE',
  path: string,
  body?: unknown,
) =>
  request<T>(path, {
    method,
    body: body !== undefined ? JSON.stringify(body) : undefined,
  })

export const api = {
  get: <T>(path: string) => request<T>(path),

  post: <T>(path: string, body?: unknown) =>
    requestWithJsonBody<T>('POST', path, body),

  put: <T>(path: string, body?: unknown) =>
    requestWithJsonBody<T>('PUT', path, body),

  delete: <T>(path: string, body?: unknown) =>
    requestWithJsonBody<T>('DELETE', path, body),
}


/** 404: user not yet in DB; 401: cookie not set yet — both expected during first registration */
export function isExpectedTokenSyncError(err: unknown): boolean {
  return err instanceof ApiError && (err.status === 404 || err.status === 401)
}

export { ApiError }
