/** Centralized fetch wrapper for the RootCoz API. */

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

/** Headers every API call sends, merged over any call-specific ones. */
function requestInit(options?: RequestInit): RequestInit {
  return {
    ...options,
    headers: {
      'Content-Type': 'application/json',
      ...options?.headers,
    },
  }
}

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const res = await fetch(path, requestInit(options))

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
  options?: RequestInit,
) =>
  request<T>(path, {
    ...options,
    method,
    body: body !== undefined ? JSON.stringify(body) : undefined,
  })

/** Result of POST /api/admin/settings/test/{integration} — always HTTP 200. */
export interface TestIntegrationResult {
  ok: boolean
  message: string
  details?: Record<string, unknown>
}

export const api = {
  get: <T>(path: string, options?: RequestInit) => request<T>(path, options),

  /**
   * Status-only GET through the same fetch path. It reports the HTTP status
   * instead of throwing on it, so a session probe can tell a 401 from a 200.
   * A network failure still rejects — that is not proof of anything.
   */
  status: async (path: string): Promise<number> => {
    const res = await fetch(path, requestInit({ credentials: 'include' }))
    return res.status
  },

  /** GET returning raw text (e.g. JSONL transcript downloads). */
  getText: async (path: string): Promise<string> => {
    const res = await fetch(path)
    // Read the body once — a failed res.json() consumes it, and reading
    // res.text() afterwards rejects with "body already used".
    const raw = await res.text()
    if (!res.ok) {
      let body: unknown = raw
      try {
        body = JSON.parse(raw)
      } catch {
        body = raw
      }
      trackFailedCall({
        status: res.status,
        endpoint: path,
        error: typeof body === 'string' ? body : body != null ? JSON.stringify(body) : '',
        timestamp: Date.now(),
      })
      throw new ApiError(res.status, res.statusText, body)
    }
    return raw
  },

  post: <T>(path: string, body?: unknown) =>
    requestWithJsonBody<T>('POST', path, body),

  put: <T>(path: string, body?: unknown) =>
    requestWithJsonBody<T>('PUT', path, body),

  /** Promote a setting's current env value into the DB (POST /api/admin/settings/{key}/persist). */
  persistSetting: (key: string) =>
    requestWithJsonBody<{ key: string; source: string }>(
      'POST',
      `/api/admin/settings/${encodeURIComponent(key)}/persist`,
    ),

  /** Test an integration connection with the currently saved settings. */
  testIntegration: (integration: string) =>
    requestWithJsonBody<TestIntegrationResult>(
      'POST',
      `/api/admin/settings/test/${encodeURIComponent(integration)}`,
    ),

  delete: <T>(path: string, body?: unknown, options?: RequestInit) =>
    requestWithJsonBody<T>('DELETE', path, body, options),
}


/** 404: user not yet in DB; 401: cookie not set yet — both expected during first registration */
export function isExpectedTokenSyncError(err: unknown): boolean {
  return err instanceof ApiError && (err.status === 404 || err.status === 401)
}

/** Extract the `detail` string from an ApiError's body, if present. */
export function extractApiDetail(err: unknown): string | null {
  if (err instanceof ApiError) {
    const detail = (err.body as { detail?: unknown })?.detail
    if (typeof detail === 'string') return detail
  }
  return null
}

export { ApiError }
