import { useState, type FormEvent } from 'react'

import { api, extractApiDetail } from '@/lib/api'
import { parseApiTimestamp } from '@/lib/utils'
import { useAuth } from '@/lib/auth'
import { Card, CardContent } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'

/**
 * What `POST /api/auth/sso/device/approve/lookup` reports about a pending code.
 * Mirrors `sso_device_approve_lookup` in `src/rootcoz/main.py`.
 */
interface DeviceCodeInfo {
  user_code: string
  provider: string | null
  client_name: string | null
  expires_at: string
  authorized: boolean
}

/**
 * Approve a CLI device code.
 *
 * The CLI prints this page's URL and a short code; the person is already signed
 * in here, sees *what* is asking (`client_name`, `provider`), and approves it.
 * Approving only names their account — the CLI redeems the code and gets a
 * session of its own, which is why this page never shows a token.
 *
 * Rendered behind `ProtectedRoute`, so a signed-out visitor lands on the login
 * page and comes back here with a **blank** code field: the code is never
 * carried in a URL, because a live one in a query string reaches the access log
 * (and, through the login return path, the SSO `next` parameter).
 */
export function DeviceApprovalPage() {
  // Whose account the CLI will become is the decision this page exists for.
  const { username } = useAuth()
  // Typed, never prefilled from a query string: a live code in a URL is written
  // to the access log, which is the whole reason the API takes it in a body.
  const [userCode, setUserCode] = useState('')
  const [info, setInfo] = useState<DeviceCodeInfo | null>(null)
  const [error, setError] = useState('')
  const [approved, setApproved] = useState(false)
  const [busy, setBusy] = useState(false)

  async function handleLookup(event: FormEvent) {
    event.preventDefault()
    setError('')
    setInfo(null)
    try {
      // POSTed, not GET: a user code in a URL would be written to the server's
      // access log, and holding one is what lets a CLI become an account.
      setInfo(
        await api.post<DeviceCodeInfo>('/api/auth/sso/device/approve/lookup', {
          user_code: userCode,
        }),
      )
    } catch (err) {
      setError(extractApiDetail(err) ?? 'That code could not be approved.')
    }
  }

  async function handleApprove() {
    setBusy(true)
    setError('')
    try {
      // The code that was *described*, not whatever is in the box now: the
      // client name the person just read must belong to the code being approved.
      await api.post('/api/auth/sso/device/approve', { user_code: info?.user_code })
      setApproved(true)
    } catch (err) {
      setError(extractApiDetail(err) ?? 'That code could not be approved.')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="flex min-h-dvh items-center justify-center bg-surface-page px-4">
      <Card className="w-full max-w-md">
        <CardContent className="space-y-4 p-6">
          <div className="space-y-1">
            <h1 className="text-lg font-semibold text-text-primary">
              Approve a command-line sign-in
            </h1>
            <p className="text-sm text-text-tertiary">
              Enter the code your terminal printed. Only approve a code you just
              started yourself.
            </p>
          </div>

          {approved ? (
            <p className="text-sm text-signal-green">
              Approved. You can close this page — your terminal continues on its
              own.
            </p>
          ) : (
            <>
              <form onSubmit={handleLookup} className="flex gap-2">
                <Input
                  value={userCode}
                  onChange={(event) => {
                    setUserCode(event.target.value)
                    // A different code means the shown client name no longer
                    // describes it.
                    setInfo(null)
                  }}
                  placeholder="ABCD-EFGH"
                  aria-label="Device code"
                  autoFocus
                />
                <Button type="submit" variant="outline" disabled={!userCode.trim()}>
                  Look up
                </Button>
              </form>

              {info && (
                <div className="space-y-3 rounded-md border border-border-default bg-surface-elevated p-3 text-sm">
                  <p className="text-text-primary">
                    <span className="font-medium">
                      {info.client_name || 'An unnamed client'}
                    </span>{' '}
                    is asking to sign in as{' '}
                    <span className="font-medium">{username}</span>.
                  </p>
                  <p className="text-text-tertiary">
                    Code {info.user_code} · provider:{' '}
                    {info.provider || 'any configured provider'} · expires{' '}
                    {parseApiTimestamp(info.expires_at).toLocaleTimeString()}
                  </p>
                  {info.authorized ? (
                    <p className="text-text-tertiary">
                      This code was already approved.
                    </p>
                  ) : (
                    <Button onClick={handleApprove} disabled={busy}>
                      {busy ? 'Approving…' : 'Approve sign-in'}
                    </Button>
                  )}
                </div>
              )}
            </>
          )}

          {error && <p className="text-sm text-signal-red">{error}</p>}
        </CardContent>
      </Card>
    </div>
  )
}
