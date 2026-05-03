import { useNavigate } from 'react-router-dom'
import { ProfileForm } from '@/components/shared/ProfileForm'
import { useAuth } from '@/lib/auth'
import { Shield } from 'lucide-react'

export function SettingsPage() {
  const navigate = useNavigate()
  const { isAdmin, role, login, refreshAuth } = useAuth()

  return (
    <div className="mx-auto max-w-md">
      <div className="mb-6 flex items-center gap-3">
        <h1 className="font-display text-lg font-bold tracking-tight text-text-primary">Settings</h1>
        {isAdmin && (
          <span className="inline-flex items-center gap-1 rounded-full bg-signal-amber/10 px-2 py-0.5 text-xs font-medium text-signal-amber">
            <Shield className="h-3 w-3" />
            {role}
          </span>
        )}
      </div>
      <ProfileForm
        onSaved={async () => { await refreshAuth(); navigate('/') }}
        onAdminLogin={async (u, k) => { await login(u, k) }}
      />
      <p className="mt-4 text-center text-xs text-text-tertiary">
        Tokens are stored locally and synced to the server (encrypted at rest) for cross-browser access.
      </p>
    </div>
  )
}
