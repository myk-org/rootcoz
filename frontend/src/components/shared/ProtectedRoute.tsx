import { Navigate } from 'react-router-dom'
import { isLoggedIn } from '@/lib/cookies'
import { useAuth } from '@/lib/auth'

interface Props {
  children: React.ReactNode
  adminOnly?: boolean
}

export function ProtectedRoute({ children, adminOnly }: Props) {
  const { isAdmin, loading, username } = useAuth()

  // Wait for auth to resolve before any redirect
  if (loading) return null

  // Use auth context username (resolves from session OR cookie)
  if (!username && !isLoggedIn()) {
    return <Navigate to="/register" replace />
  }

  if (adminOnly && !isAdmin) {
    return <Navigate to="/" replace />
  }

  return <>{children}</>
}
