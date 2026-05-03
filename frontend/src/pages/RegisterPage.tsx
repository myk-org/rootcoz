import { useEffect } from 'react'
import { useNavigate } from 'react-router-dom'
import { ProfileForm } from '@/components/shared/ProfileForm'
import { useAuth } from '@/lib/auth'
import { getUsername } from '@/lib/cookies'

export function RegisterPage() {
  const navigate = useNavigate()
  const { login, refreshAuth } = useAuth()

  // Auto-redirect if user is already identified (e.g., via SSO proxy cookie)
  useEffect(() => {
    if (getUsername()) {
      navigate('/', { replace: true })
    }
  }, [navigate])

  return (
    <div className="relative flex min-h-screen items-center justify-center bg-surface-page overflow-hidden">
      {/* Ambient grid */}
      <div className="pointer-events-none absolute inset-0 opacity-[0.03]" style={{backgroundImage: 'linear-gradient(rgba(56,139,253,.4) 1px, transparent 1px), linear-gradient(90deg, rgba(56,139,253,.4) 1px, transparent 1px)', backgroundSize: '48px 48px'}} />
      {/* Radial glow behind card */}
      <div className="pointer-events-none absolute left-1/2 top-1/2 h-[600px] w-[600px] -translate-x-1/2 -translate-y-1/2 rounded-full bg-signal-blue/[0.04] blur-3xl" />
      <div className="relative z-10 w-full max-w-md px-4">
        {/* Logo / Title block */}
        <div className="mb-8 animate-slide-up text-center">
          <div className="mx-auto mb-4 flex h-12 w-12 items-center justify-center rounded-lg border border-border-default bg-surface-card">
            <svg width="24" height="24" viewBox="0 0 24 24" fill="none" className="text-signal-blue">
              <path d="M12 2L2 7l10 5 10-5-10-5zM2 17l10 5 10-5M2 12l10 5 10-5" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
            </svg>
          </div>
          <h1 className="font-display text-xl font-bold tracking-tight text-text-primary">Jenkins Job Insight</h1>
          <p className="mt-1 text-sm text-text-tertiary">Set up your profile to continue</p>
        </div>
        {/* Form */}
        <div className="animate-slide-up [animation-delay:80ms] [animation-fill-mode:backwards]">
          <ProfileForm
            onSaved={async () => { await refreshAuth(); navigate('/') }}
            onAdminLogin={async (u, k) => { await login(u, k) }}
          />
        </div>
        <p className="mt-6 animate-slide-up text-center text-xs text-text-tertiary [animation-delay:160ms] [animation-fill-mode:backwards]">
          Tokens are stored locally and synced to the server (encrypted at rest) for cross-browser access.<br />
          Admin API key enables admin features via server-side session.
        </p>
      </div>
    </div>
  )
}
