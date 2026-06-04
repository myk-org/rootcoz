import { Navigate } from 'react-router-dom'
import { useAuth } from '@/lib/auth'

interface Props {
  children: React.ReactNode
  adminOnly?: boolean
  operatorOnly?: boolean
}

export function ProtectedRoute({ children, adminOnly, operatorOnly }: Props) {
  const { isAdmin, role, loading, authenticated } = useAuth()

  // Wait for auth to resolve before any redirect
  if (loading) return null

  if (!authenticated) {
    return <Navigate to="/login" replace />
  }

  if (adminOnly && !isAdmin) {
    return <Navigate to="/" replace />
  }

  if (operatorOnly && role !== 'operator' && role !== 'admin') {
    return <Navigate to="/" replace />
  }

  return <>{children}</>
}
