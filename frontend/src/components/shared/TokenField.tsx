import { type ReactNode } from 'react'
import { Input } from '@/components/ui/input'
import { Eye, EyeOff } from 'lucide-react'

interface TokenValidationResult {
  valid: boolean
  username: string
  message: string
}

export function TokenField({ id, label, value, onChange, show, onToggleShow, validation, error, placeholder, helpContent, optionalLabel = true, autoFocus, inputType = 'password' }: {
  id: string
  label: string
  value: string
  onChange: (value: string) => void
  show?: boolean
  onToggleShow?: () => void
  validation?: TokenValidationResult | null
  error?: string | null
  placeholder: string
  helpContent?: ReactNode
  optionalLabel?: boolean
  autoFocus?: boolean
  inputType?: 'password' | 'email' | 'text'
}) {
  return (
    <div className="space-y-1.5">
      <label htmlFor={id} className="block font-display text-xs font-medium uppercase tracking-widest text-text-secondary">
        {label} {optionalLabel && <span className="text-text-tertiary font-normal normal-case tracking-normal">(optional)</span>}
      </label>
      <div className="relative">
        <Input id={id} type={inputType !== 'password' ? inputType : show ? 'text' : 'password'} value={value} onChange={(e) => onChange(e.target.value)} placeholder={placeholder} autoComplete={inputType === 'email' ? 'email' : 'off'} autoFocus={autoFocus} className={`h-10 font-mono${inputType === 'password' ? ' pr-10' : ''}`} />
        {inputType === 'password' && onToggleShow && (
          <button type="button" onClick={onToggleShow} className="absolute right-2 top-1/2 -translate-y-1/2 p-1 text-text-tertiary hover:text-text-secondary transition-colors" aria-label={show ? 'Hide token' : 'Show token'}>
            {show ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
          </button>
        )}
      </div>
      {validation && (
        <p className={`text-xs ${validation.valid ? 'text-signal-green' : 'text-signal-red'}`}>{validation.message}</p>
      )}
      {error && (
        <p className="text-xs text-signal-red">{error}</p>
      )}
      {helpContent && <p className="text-xs text-text-tertiary">{helpContent}</p>}
    </div>
  )
}

export type { TokenValidationResult }
