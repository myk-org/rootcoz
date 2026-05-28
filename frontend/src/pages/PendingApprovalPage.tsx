import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { api } from '@/lib/api'
import { CustomMessageBanner } from '@/components/shared/CustomMessageBanner'

export function PendingApprovalPage() {
  const [loading, setLoading] = useState(true)
  const [customMessage, setCustomMessage] = useState('')
  const navigate = useNavigate()

  useEffect(() => {
    let cancelled = false
    api.get<{ custom_message?: string }>('/api/auth/pending-status')
      .then((data) => {
        if (!cancelled) {
          setLoading(false)
          if (data.custom_message) setCustomMessage(data.custom_message)
        }
      })
      .catch(() => { if (!cancelled) navigate('/login', { replace: true }) })
    return () => { cancelled = true }
  }, [navigate])

  if (loading) {
    return (
      <div className="flex min-h-screen items-center justify-center">
        <p className="text-sm text-text-secondary">Loading...</p>
      </div>
    )
  }

  return (
    <div className="flex min-h-screen items-center justify-center">
      <div className="rounded-xl border border-border-default bg-surface-card p-8 max-w-md text-center space-y-4">
        <h1 className="text-xl font-display font-bold text-text-primary">Account Pending Approval</h1>
        <p className="text-sm text-text-secondary">
          Your account has been created and is awaiting admin approval.
          You'll be able to access the application once an admin approves your registration.
        </p>
        <CustomMessageBanner message={customMessage} />
      </div>
    </div>
  )
}
