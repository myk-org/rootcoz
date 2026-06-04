import type { UserRole } from '@/types'

const ROLE_STYLES: Record<UserRole, string> = {
  admin: 'bg-signal-amber/10 text-signal-amber',
  operator: 'bg-signal-blue/10 text-signal-blue',
  reviewer: 'bg-surface-elevated text-text-secondary',
}

export function RoleBadge({ role }: { role: string }) {
  const style = ROLE_STYLES[role as UserRole] ?? ROLE_STYLES.reviewer
  return (
    <span className={`inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium ${style}`}>
      {role}
    </span>
  )
}
