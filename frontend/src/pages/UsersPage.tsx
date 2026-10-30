import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react'
import { useSearchParams } from 'react-router-dom'
import { api, ApiError } from '@/lib/api'
import { useClipboard } from '@/lib/useClipboard'
import { parsePageSize } from '@/lib/pageParams'
import { RoleBadge } from '@/components/shared/RoleBadge'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { ConfirmDialog } from '@/components/shared/ConfirmDialog'
import { Skeleton } from '@/components/ui/skeleton'
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from '@/components/ui/tooltip'
import { Copy, Check, RefreshCw, Trash2, UserPlus, Shield, CheckCircle, XCircle, Unlock, UserCheck, UserX, ChevronLeft, ChevronRight } from 'lucide-react'
import { Select, SelectTrigger, SelectValue, SelectContent, SelectItem } from '@/components/ui/select'
import { Switch } from '@/components/ui/switch'
import { formatTimestamp, formatRelativeTime, toIntInRange } from '@/lib/utils'
import type { AdminUser, AdminUsersResponse, CreateUserRequest, CreateUserResponse, RotateKeyResponse, ChangeRoleResponse, UserRole } from '@/types'
import { useAuth } from '@/lib/auth'

const PAGE_SIZE_OPTIONS = [25, 50, 100] as const
type PageSize = (typeof PAGE_SIZE_OPTIONS)[number]
const DEFAULT_PAGE_SIZE: PageSize = 25
const SEARCH_DEBOUNCE_MS = 300

/**
 * The one shape of an account-state badge: the box is identical in every state,
 * so a row never changes height when the state changes.
 */
const STATE_BADGE = {
  active: 'bg-signal-green/10 text-signal-green',
  locked: 'bg-signal-amber/10 text-signal-amber',
  pending: 'bg-signal-amber/10 text-signal-amber',
  rejected: 'bg-signal-red/10 text-signal-red',
  disabled: 'bg-surface-elevated text-text-tertiary',
} as const

function StateBadge({ state, children }: { state: keyof typeof STATE_BADGE; children: ReactNode }) {
  return (
    <span className={`inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium ${STATE_BADGE[state]}`}>
      {children}
    </span>
  )
}

/**
 * One axis for the whole row: a disabled account is disabled whatever its
 * registration status says, then a locked one, then that status. The badge and
 * the muted name read this one answer, so they cannot disagree.
 */
function accountState(user: AdminUser): keyof typeof STATE_BADGE {
  if (user.disabled) return 'disabled'
  if (user.locked) return 'locked'
  if (user.status === 'pending' || user.status === 'rejected') return user.status
  return 'active'
}

function CopyableKey({ label, value }: { label: string; value: string }) {
  const { isCopied, copy } = useClipboard()

  return (
    <div className="space-y-1.5">
      <p className="text-xs font-medium text-text-secondary">{label}</p>
      <div className="flex items-center gap-2 rounded-md border border-border-muted bg-surface-elevated px-3 py-2">
        <code className="flex-1 break-all font-mono text-xs text-text-primary">{value}</code>
        <button
          type="button"
          onClick={() => copy(value)}
          className="shrink-0 rounded p-1 text-text-tertiary transition-colors hover:text-text-secondary"
          aria-label="Copy to clipboard"
        >
          {isCopied() ? <Check className="h-4 w-4 text-signal-green" /> : <Copy className="h-4 w-4" />}
        </button>
      </div>
      <p className="text-xs text-signal-amber">
        ⚠ Save this key now — it cannot be retrieved later.
      </p>
    </div>
  )
}

export function UsersPage() {
  const { username: currentUser, refreshAuth } = useAuth()
  const [users, setUsers] = useState<AdminUser[]>([])
  // The server's count for the current needle — the only source of the total.
  const [total, setTotal] = useState(0)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  // Page bounds live in the URL, so a reload or a shared link shows the same rows.
  const [searchParams, setSearchParams] = useSearchParams()
  const limit = parsePageSize(searchParams.get('limit'), PAGE_SIZE_OPTIONS, DEFAULT_PAGE_SIZE)
  const offset = toIntInRange(searchParams.get('offset') ?? '', 0, Number.MAX_SAFE_INTEGER, 0)
  // ?user=<name> pre-fills the search, so an audit row lands on its account;
  // from then on the settled needle lives in ?q= like the page bounds do.
  const query = (searchParams.get('q') ?? searchParams.get('user') ?? '').trim()
  const [search, setSearch] = useState(query)
  // The needle this page last put in the URL: a ?q= that differs is somebody
  // else's navigation (Back/Forward, an audit deep link), so the input adopts it
  // instead of the debounce rewriting the URL back to the previous search.
  const wroteQueryRef = useRef(query)
  useEffect(() => {
    if (query === wroteQueryRef.current) return
    wroteQueryRef.current = query
    setSearch(query)
  }, [query])

  // Create dialog
  const [createOpen, setCreateOpen] = useState(false)
  const [newUsername, setNewUsername] = useState('')
  const [newUserRole, setNewUserRole] = useState<UserRole>('reviewer')
  const [newUserCanViewReports, setNewUserCanViewReports] = useState(false)
  const [newUserServerGrant, setNewUserServerGrant] = useState(false)
  const [approveTarget, setApproveTarget] = useState<string | null>(null)
  const [approveServerGrant, setApproveServerGrant] = useState(false)
  const [grantToggleInProgress, setGrantToggleInProgress] = useState<string | null>(null)
  const [creating, setCreating] = useState(false)
  const [createError, setCreateError] = useState<string | null>(null)
  const [createdUser, setCreatedUser] = useState<CreateUserResponse | null>(null)

  // Rotate key dialog
  const [rotateTarget, setRotateTarget] = useState<string | null>(null)
  const [rotating, setRotating] = useState(false)
  const [rotatedKey, setRotatedKey] = useState<RotateKeyResponse | null>(null)

  // Change role dialog
  const [roleChangeTarget, setRoleChangeTarget] = useState<{ username: string; currentRole: string; newRole: string } | null>(null)
  const [changingRole, setChangingRole] = useState(false)
  const [roleChangeResult, setRoleChangeResult] = useState<ChangeRoleResponse | null>(null)

  // Delete dialog
  const [deleteTarget, setDeleteTarget] = useState<string | null>(null)
  const [deleting, setDeleting] = useState(false)

  // Disable dialog + the in-flight guard shared by disable/enable/unlock
  const [disableTarget, setDisableTarget] = useState<string | null>(null)
  const [accountBusy, setAccountBusy] = useState<string | null>(null)
  // Denominator of "locked (3/5)" — the users list carries the count, the limit
  // lives in the login_lockout_threshold setting, so they are asked separately.
  const [lockoutThreshold, setLockoutThreshold] = useState<number | null>(null)

  // Approve/reject — single guard prevents concurrent actions on same user
  const [actionInProgress, setActionInProgress] = useState<string | null>(null)
  // Toggle can_view_reports — track username while request in flight
  const [reportsToggleInProgress, setReportsToggleInProgress] = useState<string | null>(null)

  // Action error
  const [actionError, setActionError] = useState<string | null>(null)

  /** Move the page window, keeping every other param in the URL. */
  const setPage = useCallback(
    (next: { limit?: PageSize; offset?: number }) => {
      setSearchParams((prev) => {
        const params = new URLSearchParams(prev)
        if (next.limit !== undefined) params.set('limit', String(next.limit))
        if (next.offset !== undefined) params.set('offset', String(next.offset))
        return params
      }, { replace: true })
    },
    [setSearchParams],
  )

  /**
   * Adopt a settled needle in one URL write: the query and the offset must land
   * together, or the page asks for the old window with the new filter first.
   */
  const setQuery = useCallback(
    (needle: string) => {
      wroteQueryRef.current = needle
      setSearchParams((prev) => {
        const params = new URLSearchParams(prev)
        params.delete('user')
        if (needle) params.set('q', needle)
        else params.delete('q')
        params.set('offset', '0')
        return params
      }, { replace: true })
    },
    [setSearchParams],
  )

  // Generation guard: an earlier page or search settling late must not paint
  // rows for a page the admin already left (#381).
  const usersGenerationRef = useRef(0)
  const fetchUsers = useCallback(async () => {
    const generation = ++usersGenerationRef.current
    try {
      // The server keeps no page state: every request names its own window.
      const params = new URLSearchParams({ limit: String(limit), offset: String(offset) })
      if (query) params.set('q', query)
      const data = await api.get<AdminUsersResponse>(`/api/admin/users?${params.toString()}`)
      if (generation !== usersGenerationRef.current) return
      setUsers(data.users)
      setTotal(data.total)
      setError(null)
    } catch (err) {
      if (generation !== usersGenerationRef.current) return
      setError(err instanceof Error ? err.message : 'Failed to load users')
    } finally {
      if (generation === usersGenerationRef.current) setLoading(false)
    }
  }, [limit, offset, query])

  useEffect(() => { fetchUsers() }, [fetchUsers])

  useEffect(() => {
    const needle = search.trim()
    if (needle === query) return
    // A new needle describes rows from the start, so page 3 cannot go missing.
    const timer = setTimeout(() => setQuery(needle), SEARCH_DEBOUNCE_MS)
    return () => clearTimeout(timer)
  }, [search, query, setQuery])

  // Only asked when a row is actually locked; a failed read leaves the badge at
  // the bare failure count rather than hiding the lock.
  useEffect(() => {
    if (lockoutThreshold !== null || !users.some((u) => u.locked)) return
    api
      .get<Array<{ key: string; value: string }>>('/api/admin/settings')
      .then((settings) => setLockoutThreshold(Number(settings.find((s) => s.key === 'login_lockout_threshold')?.value) || 0))
      .catch(() => {})
  }, [users, lockoutThreshold])

  async function handleCreate() {
    const trimmed = newUsername.trim()
    if (!trimmed) return
    setCreating(true)
    setCreateError(null)
    try {
      const payload: CreateUserRequest = {
        username: trimmed,
        role: newUserRole,
        // Admins always have effective reports access; ignore leftover switch state
        can_view_reports: newUserRole === 'admin' ? false : newUserCanViewReports,
        can_use_server_providers: newUserRole !== 'admin' && newUserServerGrant,
      }
      const result = await api.post<CreateUserResponse>('/api/admin/users/create', payload)
      setCreatedUser(result)
      fetchUsers()
    } catch (err) {
      if (err instanceof ApiError) {
        const body = err.body as { detail?: string } | null
        setCreateError(body?.detail ?? `Failed to create user (${err.status})`)
      } else {
        setCreateError('Failed to create user')
      }
    } finally {
      setCreating(false)
    }
  }

  async function handleRotateKey() {
    if (!rotateTarget) return
    setRotating(true)
    setActionError(null)
    try {
      const result = await api.post<RotateKeyResponse>(`/api/admin/users/${encodeURIComponent(rotateTarget)}/rotate-key`)
      setRotatedKey(result)
      setRotateTarget(null)
    } catch (err) {
      if (err instanceof ApiError) {
        const body = err.body as { detail?: string } | null
        setActionError(body?.detail ?? `Failed to rotate key (${err.status})`)
      } else {
        setActionError('Failed to rotate key')
      }
    } finally {
      setRotating(false)
    }
  }

  // Disable/enable/unlock are server-truth actions: refetch, never patch the row.
  async function handleAccountState(username: string, action: 'disable' | 'enable' | 'unlock') {
    setAccountBusy(username)
    setActionError(null)
    try {
      if (action === 'unlock') {
        await api.post(`/api/admin/users/${encodeURIComponent(username)}/unlock`)
      } else {
        await api.put(`/api/admin/users/${encodeURIComponent(username)}/disabled`, { disabled: action === 'disable' })
      }
      setDisableTarget(null)
      await fetchUsers()
    } catch (err) {
      const body = err instanceof ApiError ? (err.body as { detail?: string } | null) : null
      setActionError(body?.detail ?? `Failed to ${action} ${username}`)
    } finally {
      setAccountBusy(null)
    }
  }

  async function handleDelete() {
    if (!deleteTarget) return
    setDeleting(true)
    setActionError(null)
    try {
      await api.delete(`/api/admin/users/${encodeURIComponent(deleteTarget)}`)
      setUsers((prev) => prev.filter((u) => u.username !== deleteTarget))
      setTotal((prev) => Math.max(0, prev - 1))
      setDeleteTarget(null)
    } catch (err) {
      if (err instanceof ApiError) {
        const body = err.body as { detail?: string } | null
        setActionError(body?.detail ?? `Failed to delete user (${err.status})`)
      } else {
        setActionError('Failed to delete user')
      }
    } finally {
      setDeleting(false)
    }
  }

  function closeCreateDialog() {
    setCreateOpen(false)
    setNewUsername('')
    setNewUserRole('reviewer')
    setNewUserCanViewReports(false)
    setNewUserServerGrant(false)
    setCreateError(null)
    setCreatedUser(null)
  }

  function closeRotateDialog() {
    setRotateTarget(null)
    setRotatedKey(null)
    setActionError(null)
  }

  async function handleChangeRole() {
    if (!roleChangeTarget) return
    setChangingRole(true)
    setActionError(null)
    const newRole = roleChangeTarget.newRole
    try {
      const result = await api.put<ChangeRoleResponse>(
        `/api/admin/users/${encodeURIComponent(roleChangeTarget.username)}/role`,
        { role: newRole }
      )
      setRoleChangeResult(result)
      fetchUsers()
    } catch (err) {
      if (err instanceof ApiError) {
        const body = err.body as { detail?: string } | null
        setActionError(body?.detail ?? `Failed to change role (${err.status})`)
      } else {
        setActionError('Failed to change role')
      }
    } finally {
      setChangingRole(false)
    }
  }

  function closeRoleChangeDialog() {
    setRoleChangeTarget(null)
    setRoleChangeResult(null)
    setActionError(null)
  }

  async function handleApprove() {
    if (!approveTarget) return
    const username = approveTarget
    setActionInProgress(username)
    setActionError(null)
    try {
      await api.post(`/api/admin/users/${encodeURIComponent(username)}/approve`, { can_use_server_providers: approveServerGrant })
      setApproveTarget(null)
      setApproveServerGrant(false)
      fetchUsers()
    } catch (err) {
      if (err instanceof ApiError) {
        const body = err.body as { detail?: string } | null
        setActionError(body?.detail ?? `Failed to approve user (${err.status})`)
      } else {
        setActionError('Failed to approve user')
      }
    } finally {
      setActionInProgress(null)
    }
  }

  async function handleReject(username: string) {
    setActionInProgress(username)
    setActionError(null)
    try {
      await api.post(`/api/admin/users/${encodeURIComponent(username)}/reject`)
      fetchUsers()
    } catch (err) {
      if (err instanceof ApiError) {
        const body = err.body as { detail?: string } | null
        setActionError(body?.detail ?? `Failed to reject user (${err.status})`)
      } else {
        setActionError('Failed to reject user')
      }
    } finally {
      setActionInProgress(null)
    }
  }

  async function handleToggleServerGrant(username: string, granted: boolean) {
    setGrantToggleInProgress(username)
    setActionError(null)
    try {
      await api.put(`/api/admin/users/${encodeURIComponent(username)}/can-use-server-providers`, { can_use_server_providers: granted })
      setUsers((prev) => prev.map((u) => u.username === username ? { ...u, can_use_server_providers: granted } : u))
      if (username === currentUser) await refreshAuth()
    } catch (err) {
      setActionError(err instanceof ApiError ? (err.body as { detail?: string })?.detail ?? `Failed to update server access (${err.status})` : 'Failed to update server access')
    } finally {
      setGrantToggleInProgress(null)
    }
  }

  async function handleToggleCanViewReports(username: string, canViewReports: boolean) {
    setReportsToggleInProgress(username)
    setActionError(null)
    // Optimistic update
    setUsers((prev) =>
      prev.map((u) =>
        u.username === username ? { ...u, can_view_reports: canViewReports } : u,
      ),
    )
    try {
      await api.put(`/api/admin/users/${encodeURIComponent(username)}/can-view-reports`, {
        can_view_reports: canViewReports,
      })
      if (username === currentUser) {
        await refreshAuth()
      }
    } catch (err) {
      // Revert on failure
      setUsers((prev) =>
        prev.map((u) =>
          u.username === username ? { ...u, can_view_reports: !canViewReports } : u,
        ),
      )
      if (err instanceof ApiError) {
        const body = err.body as { detail?: string } | null
        setActionError(body?.detail ?? `Failed to update reports access (${err.status})`)
      } else {
        setActionError('Failed to update reports access')
      }
    } finally {
      setReportsToggleInProgress(null)
    }
  }

  // An empty window reports 0–0 rather than page one of nothing.
  const from = total === 0 ? 0 : offset + 1
  const to = offset + users.length

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <h1 className="font-display text-xl font-bold text-text-primary">User Management</h1>
        <div className="flex items-center gap-2">
          <Input
            placeholder="Search users…"
            aria-label="Search users"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            className="sm:w-56"
          />
          <Select
            value={String(limit)}
            onValueChange={(value) => setPage({ limit: Number(value) as PageSize, offset: 0 })}
          >
            <SelectTrigger aria-label="Rows per page" className="w-24">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {PAGE_SIZE_OPTIONS.map((size) => (
                <SelectItem key={size} value={String(size)}>
                  {size}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          <Button onClick={() => setCreateOpen(true)} className="gap-1.5">
            <UserPlus className="h-4 w-4" />
            Add User
          </Button>
        </div>
      </div>

      {/* Error */}
      {error && (
        <p role="alert" className="text-center text-signal-red py-8">{error}</p>
      )}
      {actionError && (
        <p role="alert" className="text-center text-signal-red text-sm py-2">{actionError}</p>
      )}

      {/* Table */}
      {loading ? (
        <div className="space-y-2">
          {Array.from({ length: 5 }).map((_, i) => (
            <Skeleton key={i} className="h-11 w-full" />
          ))}
        </div>
      ) : users.length === 0 && !error ? (
        <div className="flex flex-col items-center justify-center rounded-lg border border-border-muted bg-surface-card py-16 text-center">
          <p className="text-text-secondary">
            {query ? `No username matches “${query}”.` : 'No users yet.'}
          </p>
        </div>
      ) : !error && (
        <Table>
          <TableHeader>
            <TableRow className="bg-surface-card hover:bg-surface-card">
              <TableHead>Username</TableHead>
              <TableHead>Status</TableHead>
              <TableHead>Role</TableHead>
              <TableHead>View reports</TableHead>
              <TableHead>Server providers</TableHead>
              <TableHead>Created</TableHead>
              <TableHead>Last Seen</TableHead>
              <TableHead className="w-40 text-right">Actions</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {users.map((user, i) => {
              // One answer for the badge and the muting, so a row cannot look
              // half-disabled (or half-pending).
              const state = accountState(user)
              const muted = state === 'disabled'
              return (
                <TableRow
                  key={user.username}
                  className={i % 2 === 0 ? 'bg-surface-card' : 'bg-surface-elevated/40'}
                >
                  <TableCell>
                    <span className={`inline-flex items-center gap-1.5 font-mono text-sm ${muted ? 'text-text-tertiary' : 'text-text-primary'}`}>
                      {user.role === 'admin' ? (
                        <Shield className={`h-3.5 w-3.5 ${muted ? 'text-text-tertiary' : 'text-signal-amber'}`} />
                      ) : (
                        <span className={`h-2 w-2 rounded-full ${muted ? 'bg-text-tertiary' : user.role === 'operator' ? 'bg-signal-blue' : 'bg-signal-green'}`} />
                      )}
                      {user.username}
                    </span>
                  </TableCell>
                  <TableCell>
                    {/* disabled > locked > registration status (see accountState). */}
                    <StateBadge state={state}>
                      {state === 'locked'
                        ? lockoutThreshold === null
                          ? 'locked'
                          : `locked (${user.failed_login_count}/${lockoutThreshold})`
                        : state}
                    </StateBadge>
                  </TableCell>
                  <TableCell>
                    {user.username === 'admin' || user.username === currentUser ? (
                      <RoleBadge role={user.role} />
                    ) : (
                      <Select
                        value={user.role}
                        onValueChange={(newRole) => {
                          if (newRole !== user.role) {
                            setRoleChangeTarget({ username: user.username, currentRole: user.role, newRole })
                          }
                        }}
                      >
                        <SelectTrigger className="h-7 w-[96px] text-xs">
                          <SelectValue />
                        </SelectTrigger>
                        <SelectContent>
                          <SelectItem value="viewer">viewer</SelectItem>
                          <SelectItem value="reviewer">reviewer</SelectItem>
                          <SelectItem value="operator">operator</SelectItem>
                          <SelectItem value="admin">admin</SelectItem>
                        </SelectContent>
                      </Select>
                    )}
                  </TableCell>
                  <TableCell>
                    {user.role === 'admin' ? (
                      <span className="text-xs text-text-tertiary">Always</span>
                    ) : (
                      <Switch
                        checked={!!user.can_view_reports}
                        disabled={reportsToggleInProgress === user.username}
                        onCheckedChange={(checked) => handleToggleCanViewReports(user.username, checked)}
                        aria-label={`Toggle view reports for ${user.username}`}
                      />
                    )}
                  </TableCell>
                  <TableCell>
                    {user.role === 'admin' ? (
                      <span className="text-xs text-text-tertiary">Always</span>
                    ) : (
                      <Switch checked={!!user.can_use_server_providers} disabled={grantToggleInProgress === user.username || user.status !== 'active'} onCheckedChange={(checked) => handleToggleServerGrant(user.username, checked)} aria-label={`Allow server providers for ${user.username}`} />
                    )}
                  </TableCell>
                  <TableCell className="font-mono text-xs text-text-tertiary">
                    {formatTimestamp(user.created_at)}
                  </TableCell>
                  <TableCell className="font-mono text-xs text-text-tertiary">
                    {user.last_seen ? formatRelativeTime(user.last_seen) : '—'}
                  </TableCell>
                  <TableCell className="text-right">
                    {user.username === 'admin' ? (
                      <span className="text-xs text-text-tertiary italic">built-in</span>
                    ) : user.status === 'pending' ? (
                      <div className="flex items-center justify-end gap-1">
                        <TooltipProvider delayDuration={200}>
                          <Tooltip>
                            <TooltipTrigger asChild>
                              <Button
                                type="button"
                                variant="ghost"
                                size="icon"
                                aria-label={`Approve ${user.username}`}
                                className="h-7 w-7"
                                disabled={actionInProgress === user.username}
                                onClick={() => { setApproveTarget(user.username); setApproveServerGrant(!!user.can_use_server_providers) }}
                              >
                                <CheckCircle className="h-3.5 w-3.5 text-signal-green" />
                              </Button>
                            </TooltipTrigger>
                            <TooltipContent>Approve</TooltipContent>
                          </Tooltip>
                          <Tooltip>
                            <TooltipTrigger asChild>
                              <Button
                                type="button"
                                variant="ghost"
                                size="icon"
                                aria-label={`Reject ${user.username}`}
                                className="h-7 w-7"
                                disabled={actionInProgress === user.username}
                                onClick={() => handleReject(user.username)}
                              >
                                <XCircle className="h-3.5 w-3.5 text-signal-red" />
                              </Button>
                            </TooltipTrigger>
                            <TooltipContent>Reject</TooltipContent>
                          </Tooltip>
                          {user.username !== currentUser && (
                            <Tooltip>
                              <TooltipTrigger asChild>
                                <Button
                                  type="button"
                                  variant="ghost"
                                  size="icon"
                                  aria-label={`Delete ${user.username}`}
                                  className="h-7 w-7"
                                  onClick={() => setDeleteTarget(user.username)}
                                >
                                  <Trash2 className="h-3.5 w-3.5 text-text-tertiary hover:text-signal-red" />
                                </Button>
                              </TooltipTrigger>
                              <TooltipContent>Delete user</TooltipContent>
                            </Tooltip>
                          )}
                        </TooltipProvider>
                      </div>
                    ) : (
                      <div className="flex items-center justify-end gap-1">
                        <TooltipProvider delayDuration={200}>
                          {/* Account state — disable/enable, and unlock while locked */}
                          {user.username !== currentUser && user.locked && (
                            <Tooltip>
                              <TooltipTrigger asChild>
                                <Button
                                  type="button"
                                  variant="ghost"
                                  size="icon"
                                  aria-label={`Unlock ${user.username}`}
                                  className="h-7 w-7"
                                  disabled={accountBusy === user.username}
                                  onClick={() => handleAccountState(user.username, 'unlock')}
                                >
                                  <Unlock className="h-3.5 w-3.5 text-signal-green" />
                                </Button>
                              </TooltipTrigger>
                              <TooltipContent>Unlock account</TooltipContent>
                            </Tooltip>
                          )}
                          {user.username !== currentUser && (
                            <Tooltip>
                              <TooltipTrigger asChild>
                                <Button
                                  type="button"
                                  variant="ghost"
                                  size="icon"
                                  aria-label={`${user.disabled ? 'Enable' : 'Disable'} ${user.username}`}
                                  className="h-7 w-7"
                                  disabled={accountBusy === user.username}
                                  onClick={() => {
                                    if (user.disabled) handleAccountState(user.username, 'enable')
                                    else setDisableTarget(user.username)
                                  }}
                                >
                                  {user.disabled ? (
                                    <UserCheck className="h-3.5 w-3.5 text-signal-green" />
                                  ) : (
                                    <UserX className="h-3.5 w-3.5 text-signal-red" />
                                  )}
                                </Button>
                              </TooltipTrigger>
                              <TooltipContent>{user.disabled ? 'Enable account' : 'Disable account'}</TooltipContent>
                            </Tooltip>
                          )}
                          {/* Rotate key — every active user with an API key (admins and delegated users) */}
                          {user.username !== currentUser && user.status === 'active' && (
                            <Tooltip>
                              <TooltipTrigger asChild>
                                <Button
                                  type="button"
                                  variant="ghost"
                                  size="icon"
                                  aria-label={`Rotate key for ${user.username}`}
                                  className="h-7 w-7"
                                  onClick={() => setRotateTarget(user.username)}
                                >
                                  <RefreshCw className="h-3.5 w-3.5 text-text-tertiary hover:text-signal-blue" />
                                </Button>
                              </TooltipTrigger>
                              <TooltipContent>Rotate API key</TooltipContent>
                            </Tooltip>
                          )}
                          {/* Delete */}
                          {user.username !== currentUser && (
                            <Tooltip>
                              <TooltipTrigger asChild>
                                <Button
                                  type="button"
                                  variant="ghost"
                                  size="icon"
                                  aria-label={`Delete ${user.username}`}
                                  className="h-7 w-7"
                                  onClick={() => setDeleteTarget(user.username)}
                                >
                                  <Trash2 className="h-3.5 w-3.5 text-text-tertiary hover:text-signal-red" />
                                </Button>
                              </TooltipTrigger>
                              <TooltipContent>Delete user</TooltipContent>
                            </Tooltip>
                          )}
                        </TooltipProvider>
                      </div>
                    )}
                  </TableCell>
                </TableRow>
              )
            })}
          </TableBody>
        </Table>
      )}

      {/* Range + pager — always mounted once loaded, so the total never jumps in
          and out of the layout. */}
      {!loading && !error && (
        <div className="flex items-center justify-between">
          <span className="text-sm text-text-secondary" role="status">
            {from}–{to} of {total}
          </span>
          <div className="flex items-center gap-2">
            <Button
              variant="outline"
              size="sm"
              disabled={offset === 0}
              onClick={() => setPage({ offset: Math.max(0, offset - limit) })}
            >
              <ChevronLeft className="h-4 w-4" />
              Previous
            </Button>
            <Button
              variant="outline"
              size="sm"
              disabled={to >= total}
              onClick={() => setPage({ offset: offset + limit })}
            >
              Next
              <ChevronRight className="h-4 w-4" />
            </Button>
          </div>
        </div>
      )}

      {/* Create Admin Dialog */}
      <Dialog open={createOpen} onOpenChange={(open) => { if (!open) closeCreateDialog() }}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Add User</DialogTitle>
            <DialogDescription>
              Create a new user. Admin users will receive an API key automatically.
            </DialogDescription>
          </DialogHeader>
          {createdUser ? (
            <div className="space-y-4 py-2">
              <p className="text-sm text-text-secondary">
                {newUserRole.charAt(0).toUpperCase() + newUserRole.slice(1)}{' '}
                <span className="font-mono font-medium text-text-primary">{createdUser.username}</span> created successfully.
              </p>
              {createdUser.api_key && (
                <CopyableKey label="API Key" value={createdUser.api_key} />
              )}
            </div>
          ) : (
            <div className="space-y-4 py-2">
              <div className="space-y-1.5">
                <label htmlFor="new-username" className="block text-sm font-medium text-text-secondary">
                  Username
                </label>
                <Input
                  id="new-username"
                  value={newUsername}
                  onChange={(e) => { setNewUsername(e.target.value); setCreateError(null) }}
                  placeholder="e.g. john.doe"
                  autoFocus
                  className="font-mono"
                />
                {createError && (
                  <p className="text-xs text-signal-red">{createError}</p>
                )}
              </div>
              <div className="space-y-1.5">
                <label className="block text-xs font-medium text-text-secondary">Role</label>
                <Select
                  value={newUserRole}
                  onValueChange={(v) => {
                    const role = v as UserRole
                    setNewUserRole(role)
                    if (role === 'admin') {
                      setNewUserCanViewReports(false)
                      setNewUserServerGrant(false)
                    }
                  }}
                >
                  <SelectTrigger className="h-10">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="viewer">Viewer</SelectItem>
                    <SelectItem value="reviewer">Reviewer</SelectItem>
                    <SelectItem value="operator">Operator</SelectItem>
                    <SelectItem value="admin">Admin</SelectItem>
                  </SelectContent>
                </Select>
              </div>
              {newUserRole !== 'admin' && (
                <div className="flex items-center justify-between gap-3">
                  <label htmlFor="new-user-server-grant" className="text-sm text-text-secondary">Allow server providers</label>
                  <Switch id="new-user-server-grant" checked={newUserServerGrant} onCheckedChange={setNewUserServerGrant} aria-label="Allow server providers" />
                </div>
              )}
              {newUserRole !== 'admin' && (
                <div className="flex items-center justify-between gap-3">
                  <label htmlFor="new-user-can-view-reports" className="text-sm text-text-secondary">
                    Allow viewing reports
                  </label>
                  <Switch
                    id="new-user-can-view-reports"
                    checked={newUserCanViewReports}
                    onCheckedChange={setNewUserCanViewReports}
                    aria-label="Allow viewing reports"
                  />
                </div>
              )}
            </div>
          )}
          <DialogFooter>
            {createdUser ? (
              <Button onClick={closeCreateDialog}>Done</Button>
            ) : (
              <>
                <Button variant="ghost" onClick={closeCreateDialog} disabled={creating}>
                  Cancel
                </Button>
                <Button onClick={handleCreate} disabled={!newUsername.trim() || creating}>
                  {creating ? 'Creating...' : 'Create'}
                </Button>
              </>
            )}
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog open={approveTarget !== null} onOpenChange={(open) => { if (!open && !actionInProgress) { setApproveTarget(null); setActionError(null) } }}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Approve {approveTarget}</DialogTitle>
            <DialogDescription>Choose whether this user can use server AI credentials. Without access, they can use their own key.</DialogDescription>
          </DialogHeader>
          <div className="flex items-center justify-between gap-3 py-3">
            <label htmlFor="approve-server-grant" className="text-sm text-text-secondary">Allow server providers</label>
            <Switch id="approve-server-grant" checked={approveServerGrant} onCheckedChange={setApproveServerGrant} aria-label="Allow server providers on approval" />
          </div>
          <DialogFooter>
            <Button variant="ghost" onClick={() => setApproveTarget(null)} disabled={!!actionInProgress}>Cancel</Button>
            <Button onClick={handleApprove} disabled={!!actionInProgress}>{actionInProgress ? 'Approving...' : 'Approve user'}</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Rotate Key Confirmation */}
      <ConfirmDialog
        open={rotateTarget !== null && rotatedKey === null}
        onOpenChange={(open) => { if (!open && !rotating) closeRotateDialog() }}
        title="Rotate API Key"
        description={`Generate a new API key for "${rotateTarget}"? The current key is invalidated immediately and cannot be restored.`}
        confirmLabel="Rotate Key"
        onConfirm={handleRotateKey}
        loading={rotating}
        error={actionError}
      />

      {/* Rotate Key Result — the new key is shown exactly once */}
      <Dialog open={rotatedKey !== null} onOpenChange={(open) => { if (!open) closeRotateDialog() }}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>New API Key</DialogTitle>
            <DialogDescription>
              This is the only time the new key for {rotatedKey?.username} is shown.
            </DialogDescription>
          </DialogHeader>
          {rotatedKey && (
            <div className="py-2">
              <CopyableKey label="New API Key" value={rotatedKey.new_api_key} />
            </div>
          )}
          <DialogFooter>
            <Button onClick={closeRotateDialog}>Done</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Change Role Dialog */}
      <Dialog open={roleChangeTarget !== null} onOpenChange={(open) => { if (!open) closeRoleChangeDialog() }}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>
              Change Role to {roleChangeTarget?.newRole}
            </DialogTitle>
            <DialogDescription>
              {roleChangeResult
                ? (roleChangeResult.api_key
                    ? `${roleChangeResult.username} is now ${roleChangeResult.role}. Save the API key below.`
                    : `${roleChangeResult.username} is now ${roleChangeResult.role}.`)
                : `Change "${roleChangeTarget?.username}" from ${roleChangeTarget?.currentRole} to ${roleChangeTarget?.newRole}?`
              }
            </DialogDescription>
          </DialogHeader>
          {roleChangeResult?.api_key ? (
            <div className="py-2">
              <CopyableKey label="API Key" value={roleChangeResult.api_key} />
            </div>
          ) : null}
          {actionError && !roleChangeResult && (
            <p className="text-xs text-signal-red px-1">{actionError}</p>
          )}
          <DialogFooter>
            {roleChangeResult ? (
              <Button onClick={closeRoleChangeDialog}>Done</Button>
            ) : (
              <>
                <Button variant="ghost" onClick={closeRoleChangeDialog} disabled={changingRole}>
                  Cancel
                </Button>
                <Button
                  onClick={handleChangeRole}
                  disabled={changingRole}
                  variant={roleChangeTarget?.newRole === 'viewer' || roleChangeTarget?.newRole === 'reviewer' ? 'destructive' : 'default'}
                >
                  {changingRole ? 'Changing...' : 'Change Role'}
                </Button>
              </>
            )}
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Delete Confirm Dialog */}
      <ConfirmDialog
        open={deleteTarget !== null}
        onOpenChange={(open) => { if (!open) setDeleteTarget(null) }}
        title="Delete user"
        description={`Permanently delete "${deleteTarget}"? This will revoke their admin access.`}
        confirmLabel="Delete"
        variant="destructive"
        onConfirm={handleDelete}
        loading={deleting}
      />

      {/* Disable Confirm Dialog — reversible, but it stops the account logging in */}
      <ConfirmDialog
        open={disableTarget !== null}
        onOpenChange={(open) => { if (!open && !accountBusy) { setDisableTarget(null); setActionError(null) } }}
        title="Disable account"
        description={`Disable "${disableTarget}"? They cannot log in until you enable the account again. Their history, comments and jobs stay intact.`}
        confirmLabel="Disable"
        variant="destructive"
        onConfirm={() => { if (disableTarget) handleAccountState(disableTarget, 'disable') }}
        loading={accountBusy !== null}
        error={actionError}
      />
    </div>
  )
}
