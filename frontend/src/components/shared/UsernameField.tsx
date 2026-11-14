import { Input } from '@/components/ui/input'

interface UsernameFieldProps {
  id: string
  value: string
  onChange: (value: string) => void
  autoFocus?: boolean
}

/**
 * The username input used by every form on the sign-in page.
 *
 * Login, register, "create account" and "I already have an account" all ask for
 * the same thing; one component keeps the label, the hint and the control style
 * identical across the four, and lets a future change land once.
 */
export function UsernameField({ id, value, onChange, autoFocus }: UsernameFieldProps) {
  return (
    <div className="space-y-1.5">
      <label htmlFor={id} className="block font-display text-xs font-medium uppercase tracking-widest text-text-secondary">
        Username
      </label>
      <Input
        id={id}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder="e.g. jdoe"
        autoComplete="username"
        autoFocus={autoFocus}
        className="font-mono"
      />
    </div>
  )
}
