import { Navigate, useLocation } from 'react-router-dom'
import { useAuth } from '@/lib/auth'

interface Props {
  children: React.ReactNode
  adminOnly?: boolean
  operatorOnly?: boolean
  reviewerOnly?: boolean
  /** Allow access when canViewReports OR isAdmin (backend always grants admins reports access). */
  reportsAccess?: boolean
}

export function ProtectedRoute({ children, adminOnly, operatorOnly, reviewerOnly, reportsAccess }: Props) {
  const { isAdmin, canViewReports, role, loading, authenticated } = useAuth()
  const location = useLocation()

  // Wait for auth to resolve before any redirect
  if (loading) return null

  if (!authenticated) {
    return <Navigate to="/login" state={{ from: location }} replace />
  }

  if (adminOnly && !isAdmin) {
    return <Navigate to="/" replace />
  }

  if (reportsAccess && !(isAdmin || canViewReports)) {
    return <Navigate to="/" replace />
  }

  if (operatorOnly && role !== 'operator' && role !== 'admin') {
    return <Navigate to="/" replace />
  }

  if (reviewerOnly && role === 'viewer') {
    return <Navigate to="/" replace />
  }

  return <>{children}</>
}
