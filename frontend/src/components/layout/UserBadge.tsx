import { useNavigate } from 'react-router-dom'
import { useAuth } from '@/lib/auth'
import { LogOut, Settings, Shield } from 'lucide-react'

export function UserBadge() {
  const { username, isAdmin, logout } = useAuth()
  const navigate = useNavigate()
  if (!username) return null

  async function handleLogout() {
    await logout()
    navigate('/register')
  }

  return (
    <div className="flex items-center gap-2 rounded-full bg-surface-elevated px-3 py-1 text-sm text-text-secondary">
      {isAdmin ? (
        <Shield className="h-3 w-3 text-signal-amber" />
      ) : (
        <div className="h-2 w-2 rounded-full bg-signal-green" />
      )}
      <span className="font-mono text-xs">{username}</span>
      {isAdmin && (
        <span className="rounded bg-signal-amber/10 px-1 py-px text-[10px] font-medium text-signal-amber">
          Admin
        </span>
      )}
      <button type="button" aria-label="Settings" onClick={() => navigate('/settings')}
        className="ml-1 rounded-sm p-0.5 text-text-tertiary transition-colors hover:text-signal-blue" title="Settings">
        <Settings className="h-3 w-3" />
      </button>
      <button type="button" aria-label="Logout" onClick={handleLogout}
        className="rounded-sm p-0.5 text-text-tertiary transition-colors hover:text-signal-red" title="Logout">
        <LogOut className="h-3 w-3" />
      </button>
    </div>
  )
}
