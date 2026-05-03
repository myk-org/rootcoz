import { useCallback, useEffect, useState } from 'react'
import { api, ApiError } from '@/lib/api'
import { useClipboard } from '@/lib/useClipboard'
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
import { Copy, Check, RefreshCw, Trash2, UserPlus, Shield } from 'lucide-react'
import { Select, SelectTrigger, SelectValue, SelectContent, SelectItem } from '@/components/ui/select'
import { formatTimestamp } from '@/lib/utils'
import type { AdminUser, CreateUserResponse, RotateKeyResponse, ChangeRoleResponse } from '@/types'

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
  const [users, setUsers] = useState<AdminUser[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  // Create dialog
  const [createOpen, setCreateOpen] = useState(false)
  const [newUsername, setNewUsername] = useState('')
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

  // Action error
  const [actionError, setActionError] = useState<string | null>(null)

  const fetchUsers = useCallback(async () => {
    try {
      const data = await api.get<{ users: AdminUser[] }>('/api/admin/users')
      setUsers(data.users)
      setError(null)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load users')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { fetchUsers() }, [fetchUsers])

  async function handleCreate() {
    const trimmed = newUsername.trim()
    if (!trimmed) return
    setCreating(true)
    setCreateError(null)
    try {
      const result = await api.post<CreateUserResponse>('/api/admin/users', { username: trimmed })
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

  async function handleDelete() {
    if (!deleteTarget) return
    setDeleting(true)
    setActionError(null)
    try {
      await api.delete(`/api/admin/users/${encodeURIComponent(deleteTarget)}`)
      setUsers((prev) => prev.filter((u) => u.username !== deleteTarget))
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

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="font-display text-xl font-bold text-text-primary">User Management</h1>
          <p className="mt-0.5 text-sm text-text-tertiary">
            {users.length} {users.length === 1 ? 'user' : 'users'}
          </p>
        </div>
        <Button onClick={() => setCreateOpen(true)} className="gap-1.5">
          <UserPlus className="h-4 w-4" />
          Create Admin
        </Button>
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
          <p className="text-text-secondary">No users yet.</p>
        </div>
      ) : !error && (
        <Table>
          <TableHeader>
            <TableRow className="bg-surface-card hover:bg-surface-card">
              <TableHead>Username</TableHead>
              <TableHead>Role</TableHead>
              <TableHead>Created</TableHead>
              <TableHead>Last Seen</TableHead>
              <TableHead className="w-40 text-right">Actions</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {users.map((user, i) => (
              <TableRow
                key={user.username}
                className={i % 2 === 0 ? 'bg-surface-card' : 'bg-surface-elevated/40'}
              >
                <TableCell>
                  <span className="inline-flex items-center gap-1.5 font-mono text-sm text-text-primary">
                    {user.role === 'admin' ? (
                      <Shield className="h-3.5 w-3.5 text-signal-amber" />
                    ) : (
                      <span className="h-2 w-2 rounded-full bg-signal-green" />
                    )}
                    {user.username}
                  </span>
                </TableCell>
                <TableCell>
                  {user.role === 'admin' ? (
                    <span className="inline-flex items-center rounded-full bg-signal-amber/10 px-2 py-0.5 text-xs font-medium text-signal-amber">
                      admin
                    </span>
                  ) : (
                    <span className="inline-flex items-center rounded-full bg-surface-elevated px-2 py-0.5 text-xs font-medium text-text-secondary">
                      user
                    </span>
                  )}
                </TableCell>
                <TableCell className="font-mono text-xs text-text-tertiary">
                  {formatTimestamp(user.created_at)}
                </TableCell>
                <TableCell className="font-mono text-xs text-text-tertiary">
                  {user.last_seen ? formatTimestamp(user.last_seen) : '—'}
                </TableCell>
                <TableCell className="text-right">
                  {user.username === 'admin' ? (
                    <span className="text-xs text-text-tertiary italic">built-in</span>
                  ) : (
                    <div className="flex items-center justify-end gap-1">
                      {/* Role select */}
                      <Select
                        value={user.role}
                        onValueChange={(newRole) => {
                          if (newRole !== user.role) {
                            setRoleChangeTarget({ username: user.username, currentRole: user.role, newRole })
                          }
                        }}
                      >
                        <SelectTrigger className="h-7 w-[80px] text-xs">
                          <SelectValue />
                        </SelectTrigger>
                        <SelectContent>
                          <SelectItem value="user">user</SelectItem>
                          <SelectItem value="admin">admin</SelectItem>
                        </SelectContent>
                      </Select>
                      {/* Rotate key — only for admins (regular users have no API key) */}
                      <Button
                        type="button"
                        variant="ghost"
                        size="icon"
                        aria-label={`Rotate key for ${user.username}`}
                        className={`h-7 w-7${user.role !== 'admin' ? ' invisible' : ''}`}
                        title="Rotate API key"
                        disabled={user.role !== 'admin'}
                        onClick={() => setRotateTarget(user.username)}
                      >
                        <RefreshCw className="h-3.5 w-3.5 text-text-tertiary hover:text-signal-blue" />
                      </Button>
                      {/* Delete */}
                      <Button
                        type="button"
                        variant="ghost"
                        size="icon"
                        aria-label={`Delete ${user.username}`}
                        className="h-7 w-7"
                        title="Delete user"
                        onClick={() => setDeleteTarget(user.username)}
                      >
                        <Trash2 className="h-3.5 w-3.5 text-text-tertiary hover:text-signal-red" />
                      </Button>
                    </div>
                  )}
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}

      {/* Create Admin Dialog */}
      <Dialog open={createOpen} onOpenChange={(open) => { if (!open) closeCreateDialog() }}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Create Admin User</DialogTitle>
            <DialogDescription>
              Create a new admin user. An API key will be generated automatically.
            </DialogDescription>
          </DialogHeader>
          {createdUser ? (
            <div className="space-y-4 py-2">
              <p className="text-sm text-text-secondary">
                Admin user <span className="font-mono font-medium text-text-primary">{createdUser.username}</span> created successfully.
              </p>
              <CopyableKey label="API Key" value={createdUser.api_key} />
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
                  placeholder="e.g. admin"
                  autoFocus
                  className="font-mono"
                />
                {createError && (
                  <p className="text-xs text-signal-red">{createError}</p>
                )}
              </div>
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

      {/* Rotate Key Dialog */}
      <Dialog open={rotateTarget !== null} onOpenChange={(open) => { if (!open) closeRotateDialog() }}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Rotate API Key</DialogTitle>
            <DialogDescription>
              {rotatedKey
                ? `New API key for ${rotatedKey.username}:`
                : `Generate a new API key for "${rotateTarget}"? The old key will stop working immediately.`
              }
            </DialogDescription>
          </DialogHeader>
          {rotatedKey ? (
            <div className="py-2">
              <CopyableKey label="New API Key" value={rotatedKey.new_api_key} />
            </div>
          ) : null}
          {actionError && !rotatedKey && (
            <p className="text-xs text-signal-red px-1">{actionError}</p>
          )}
          <DialogFooter>
            {rotatedKey ? (
              <Button onClick={closeRotateDialog}>Done</Button>
            ) : (
              <>
                <Button variant="ghost" onClick={closeRotateDialog} disabled={rotating}>
                  Cancel
                </Button>
                <Button onClick={handleRotateKey} disabled={rotating}>
                  {rotating ? 'Rotating...' : 'Rotate Key'}
                </Button>
              </>
            )}
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Change Role Dialog */}
      <Dialog open={roleChangeTarget !== null} onOpenChange={(open) => { if (!open) closeRoleChangeDialog() }}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>
              {roleChangeTarget?.newRole === 'admin' ? 'Promote to Admin' : 'Demote to User'}
            </DialogTitle>
            <DialogDescription>
              {roleChangeResult
                ? (roleChangeResult.api_key
                    ? `${roleChangeResult.username} is now an admin. Save the API key below.`
                    : `${roleChangeResult.username} has been demoted to regular user.`)
                : (roleChangeTarget?.newRole === 'admin'
                    ? `Promote "${roleChangeTarget?.username}" to admin? An API key will be generated.`
                    : `Demote "${roleChangeTarget?.username}" to regular user? Their API key will be revoked and admin sessions invalidated.`)
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
                  variant={roleChangeTarget?.newRole === 'user' ? 'destructive' : 'default'}
                >
                  {changingRole
                    ? 'Changing...'
                    : (roleChangeTarget?.newRole === 'admin' ? 'Promote' : 'Demote')
                  }
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
    </div>
  )
}
