import { useNavigate } from 'react-router-dom'
import { useAuth } from '@/lib/auth'
import { RoleBadge } from '@/components/shared/RoleBadge'
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from '@/components/ui/tooltip'
import { LogOut, Settings, Shield } from 'lucide-react'

export function UserBadge() {
  const { username, role, logout } = useAuth()
  const navigate = useNavigate()
  if (!username) return null

  async function handleLogout() {
    await logout()
    navigate('/login')
  }

  return (
    <div className="flex items-center gap-2 rounded-full bg-surface-elevated px-3 py-1 text-sm text-text-secondary">
      {role === 'admin' ? (
        <Shield className="h-3 w-3 text-signal-amber" />
      ) : (
        <div className={`h-2 w-2 rounded-full ${role === 'operator' ? 'bg-signal-blue' : 'bg-signal-green'}`} />
      )}
      <span className="font-mono text-xs">{username}</span>
      <RoleBadge role={role} />
      <TooltipProvider delayDuration={200}>
        <Tooltip>
          <TooltipTrigger asChild>
            <button type="button" aria-label="Settings" onClick={() => navigate('/settings')}
              className="ml-1 rounded-sm p-0.5 text-text-tertiary transition-colors hover:text-signal-blue">
              <Settings className="h-3 w-3" />
            </button>
          </TooltipTrigger>
          <TooltipContent>Settings</TooltipContent>
        </Tooltip>
        <Tooltip>
          <TooltipTrigger asChild>
            <button type="button" aria-label="Logout" onClick={handleLogout}
              className="rounded-sm p-0.5 text-text-tertiary transition-colors hover:text-signal-red">
              <LogOut className="h-3 w-3" />
            </button>
          </TooltipTrigger>
          <TooltipContent>Logout</TooltipContent>
        </Tooltip>
      </TooltipProvider>
    </div>
  )
}
